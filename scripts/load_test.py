"""External HTTP load tester (Requirement #9) — hits the LIVE server.

Unlike `manage.py stress_test` (which calls the view in-process via DRF's
APIClient), this is a true external client: it sends real HTTP requests over the
network to a running server / load balancer, exactly like JMeter or Locust. It
reports the test-tool metrics the brief asks for:

    Total Requests · Success Requests · Failed Requests
    Average Response Time · System crashed or not

Start the server(s) first, then point this at them. Examples:

    # Single dev server:
    python manage.py runserver 127.0.0.1:8000
    python scripts/load_test.py --base-url http://127.0.0.1:8000 --requests 200

    # The real multi-instance setup from Req 5 (round-robin across instances):
    python scripts/start_instances.py --ports 8001 8002 8003
    python scripts/load_test.py --targets http://127.0.0.1:8001 http://127.0.0.1:8002 http://127.0.0.1:8003

    # Compare load patterns:
    python scripts/load_test.py --mode both

By default it loads `/api/whoami/` (no auth, present on every instance). Use
`--endpoint checkout` to drive the real authenticated checkout write-path; that
mode registers users and seeds stock over HTTP first (see --help).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import itertools
import statistics
import time

import requests


# ── routing across one or more live targets (round-robin) ────────────────────
class Targets:
    def __init__(self, base_urls: list[str]):
        self.base_urls = [u.rstrip("/") for u in base_urls]
        self._cycle = itertools.cycle(self.base_urls)

    def next(self) -> str:
        return next(self._cycle)


def percentile(sorted_vals: list[float], q: float):
    if not sorted_vals:
        return None
    k = min(len(sorted_vals) - 1, int(round((len(sorted_vals) - 1) * q)))
    return sorted_vals[k]


# ── one request unit ─────────────────────────────────────────────────────────
def make_request(session: requests.Session, method: str, url: str,
                 timeout: float, **kw) -> dict:
    t0 = time.perf_counter()
    try:
        resp = session.request(method, url, timeout=timeout, **kw)
        dt = (time.perf_counter() - t0) * 1000
        return {"ok": 200 <= resp.status_code < 300, "status": resp.status_code,
                "latency_ms": dt, "error": None}
    except requests.RequestException as exc:
        dt = (time.perf_counter() - t0) * 1000
        # A connection refused / reset / timeout is how a CRASHED or overwhelmed
        # server shows up to an external client.
        return {"ok": False, "status": None, "latency_ms": dt,
                "error": type(exc).__name__}


# ── analysis ─────────────────────────────────────────────────────────────────
def analyse(records: list[dict], wall_ms: float, mode: str, endpoint: str) -> dict:
    total = len(records)
    ok = [r for r in records if r["ok"]]
    failed = [r for r in records if not r["ok"]]
    conn_errors = [r for r in failed if r["error"] is not None]
    server_5xx = [r for r in failed if isinstance(r["status"], int) and r["status"] >= 500]
    lat = sorted(r["latency_ms"] for r in ok)
    # "System crashed" from a client's view = connection errors or 5xx responses.
    crashed = bool(conn_errors or server_5xx)
    return {
        "mode": mode,
        "endpoint": endpoint,
        "total": total,
        "success": len(ok),
        "failed": len(failed),
        "conn_errors": len(conn_errors),
        "server_5xx": len(server_5xx),
        "status_breakdown": _status_counts(records),
        "avg_ms": statistics.fmean(lat) if lat else None,
        "p50_ms": percentile(lat, 0.50),
        "p95_ms": percentile(lat, 0.95),
        "p99_ms": percentile(lat, 0.99),
        "max_ms": lat[-1] if lat else None,
        "wall_ms": round(wall_ms),
        "throughput_rps": round(total / (wall_ms / 1000), 1) if wall_ms else 0.0,
        "crashed": crashed,
    }


def _status_counts(records: list[dict]) -> dict:
    counts: dict = {}
    for r in records:
        key = r["status"] if r["status"] is not None else f"ERR:{r['error']}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: str(kv[0])))


# ── runners ──────────────────────────────────────────────────────────────────
def run_concurrent(call_one, total: int, concurrency: int) -> tuple[list, float]:
    records: list = [None] * total
    wall0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(call_one, i): i for i in range(total)}
        for fut in concurrent.futures.as_completed(futures):
            records[futures[fut]] = fut.result()
    return records, (time.perf_counter() - wall0) * 1000


def run_sequential(call_one, total: int) -> tuple[list, float]:
    wall0 = time.perf_counter()
    records = [call_one(i) for i in range(total)]
    return records, (time.perf_counter() - wall0) * 1000


# ── checkout-path setup over HTTP (optional, heavier) ────────────────────────
def _find_product_id(base: str, timeout: float):
    """Return the id of an in-stock product from the live catalogue, or None."""
    r = requests.get(f"{base}/api/products/", timeout=timeout)
    if r.status_code != 200:
        return None
    data = r.json()
    items = data.get("results", data) if isinstance(data, dict) else data
    for p in items:
        if p.get("stock", 0) > 0:
            return p["id"]
    return items[0]["id"] if items else None


def setup_checkout_users(targets: Targets, count: int, timeout: float) -> list[dict]:
    """Register `count` users over HTTP, log each in for a JWT, and put one unit
    of an in-stock product in their cart — so each is ready to POST /api/checkout/.

    Requires a seeded in-stock product; create one with:
        python manage.py load_test_seed --stock <N>
    """
    base0 = targets.base_urls[0]
    product_id = _find_product_id(base0, timeout)
    if product_id is None:
        raise SystemExit("No product found. Seed one first: "
                         "python manage.py load_test_seed --stock 1000")

    sess = requests.Session()
    prepared = []
    for i in range(count):
        base = targets.next()
        uname = f"loadtest_user_{i}"
        pwd = "loadtest-pass-123"
        # Register (ignore 400 = already exists from a previous run).
        sess.post(f"{base}/api/auth/register/",
                  json={"username": uname, "email": f"{uname}@example.com",
                        "password": pwd}, timeout=timeout)
        # Login for a JWT access token.
        r = sess.post(f"{base}/api/auth/login/",
                      json={"username": uname, "password": pwd}, timeout=timeout)
        if r.status_code != 200:
            continue
        token = r.json().get("access")
        headers = {"Authorization": f"Bearer {token}"}
        # Put one unit in the cart so checkout has something to process.
        sess.post(f"{base}/api/cart/items/",
                  json={"product": product_id, "quantity": 1},
                  headers=headers, timeout=timeout)
        prepared.append({"token": token, "base_url": base})
    return prepared


# ── reporting ────────────────────────────────────────────────────────────────
def print_summary(s: dict) -> None:
    def ms(v):
        return f"{v:.1f} ms" if v is not None else "—"

    bar = "=" * 70
    print()
    print(bar)
    print(f" EXTERNAL HTTP LOAD TEST  [endpoint: {s['endpoint']} | mode: {s['mode'].upper()}]")
    print(bar)
    print(f"  Total Requests        : {s['total']}")
    print(f"  Success Requests      : {s['success']}")
    print(f"  Failed Requests       : {s['failed']}  "
          f"(conn errors: {s['conn_errors']}, 5xx: {s['server_5xx']})")
    print(f"  Average Response Time : {ms(s['avg_ms'])}")
    print(f"  System crashed        : {'YES' if s['crashed'] else 'NO'}")
    print("-" * 70)
    print(f"  Latency  avg {ms(s['avg_ms'])}  p50 {ms(s['p50_ms'])}  "
          f"p95 {ms(s['p95_ms'])}  p99 {ms(s['p99_ms'])}  max {ms(s['max_ms'])}")
    print(f"  Wall {s['wall_ms']} ms   Throughput {s['throughput_rps']} req/s")
    print(f"  Status breakdown      : {s['status_breakdown']}")
    print(bar)


def print_comparison(seq: dict, con: dict) -> None:
    def ms(v):
        return f"{v:.1f} ms" if v is not None else "—"
    bar = "=" * 70
    print()
    print(bar)
    print(" SEQUENTIAL vs CONCURRENT — external HTTP load")
    print(bar)
    print(f"  {'Metric':<24}{'SEQUENTIAL':>16}{'CONCURRENT':>16}")
    print(f"  {'-'*24}{'-'*16:>16}{'-'*16:>16}")
    print(f"  {'Total Requests':<24}{seq['total']:>16}{con['total']:>16}")
    print(f"  {'Success Requests':<24}{seq['success']:>16}{con['success']:>16}")
    print(f"  {'Failed Requests':<24}{seq['failed']:>16}{con['failed']:>16}")
    print(f"  {'Average Response Time':<24}{ms(seq['avg_ms']):>16}{ms(con['avg_ms']):>16}")
    print(f"  {'Throughput (req/s)':<24}{seq['throughput_rps']:>16}{con['throughput_rps']:>16}")
    print(f"  {'System crashed':<24}{('YES' if seq['crashed'] else 'NO'):>16}{('YES' if con['crashed'] else 'NO'):>16}")
    print(bar)


# ── main ─────────────────────────────────────────────────────────────────────
def build_caller(endpoint: str, targets: Targets, timeout: float, prepared: list):
    """Return a `call_one(i)` closure for the chosen endpoint."""
    if endpoint == "whoami":
        sess = requests.Session()

        def call_one(_i):
            return make_request(sess, "GET", targets.next() + "/api/whoami/", timeout)
        return call_one

    if endpoint == "checkout":
        if not prepared:
            raise SystemExit("checkout mode needs prepared users; setup failed "
                             "(is a product seeded with stock?).")
        sess = requests.Session()

        def call_one(i):
            u = prepared[i % len(prepared)]
            headers = {"Authorization": f"Bearer {u['token']}"}
            return make_request(
                sess, "POST", u["base_url"] + "/api/checkout/", timeout,
                json={"shipping_address": "123 Load Test Ave"}, headers=headers,
            )
        return call_one

    raise SystemExit(f"unknown endpoint: {endpoint}")


def run_one(mode, endpoint, targets, total, concurrency, timeout, prepared):
    call_one = build_caller(endpoint, targets, timeout, prepared)
    if mode == "concurrent":
        records, wall = run_concurrent(call_one, total, concurrency)
    else:
        records, wall = run_sequential(call_one, total)
    s = analyse(records, wall, mode, endpoint)
    print_summary(s)
    return s


def main():
    p = argparse.ArgumentParser(description="External HTTP load test (Req 9).")
    p.add_argument("--base-url", default="http://127.0.0.1:8000",
                   help="Single target base URL (used if --targets not given).")
    p.add_argument("--targets", nargs="+",
                   help="Multiple target base URLs to round-robin across "
                        "(e.g. the Req 5 instances).")
    p.add_argument("--endpoint", choices=["whoami", "checkout"], default="whoami")
    p.add_argument("--mode", choices=["concurrent", "sequential", "both"],
                   default="concurrent")
    p.add_argument("--requests", type=int, default=200)
    p.add_argument("--concurrency", type=int, default=20)
    p.add_argument("--timeout", type=float, default=10.0)
    args = p.parse_args()

    base_urls = args.targets if args.targets else [args.base_url]
    targets = Targets(base_urls)

    print("Probing targets:", ", ".join(base_urls))
    # Liveness check — fail clearly if nothing is listening.
    live = []
    for u in base_urls:
        try:
            r = requests.get(u.rstrip("/") + "/api/whoami/", timeout=3)
            if r.status_code == 200:
                live.append(u)
        except requests.RequestException:
            pass
    if not live:
        raise SystemExit(
            "No live server found. Start one first:\n"
            "  python manage.py runserver 127.0.0.1:8000\n"
            "or the Req 5 instances:\n"
            "  python scripts/start_instances.py --ports 8001 8002 8003")
    print(f"Live targets: {', '.join(live)}")

    prepared = []
    if args.endpoint == "checkout":
        print(f"Preparing {args.requests} checkout users over HTTP …")
        prepared = setup_checkout_users(targets, args.requests, args.timeout)
        print(f"Prepared {len(prepared)} authenticated users.")

    modes = ["sequential", "concurrent"] if args.mode == "both" else [args.mode]
    results = {}
    for m in modes:
        results[m] = run_one(m, args.endpoint, targets, args.requests,
                             args.concurrency, args.timeout, prepared)

    if args.mode == "both":
        print_comparison(results["sequential"], results["concurrent"])


if __name__ == "__main__":
    main()
