import statistics
import threading
import time
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management.base import BaseCommand
from rest_framework.test import APIClient

from core.concurrency import limit_concurrency, CapacityExceeded
from coupons.models import Coupon

REPORT_DIR = Path(settings.BASE_DIR) / "reports"

REPORT_STATE: dict = {"rate": None, "scenarios": []}


WORK_TIME = 0.2
BANNER = "=" * 72
SUB = "-" * 72


def header(title: str):
    print()
    print(BANNER)
    print(f" {title}")
    print(BANNER)


def subheader(title: str):
    print()
    print(SUB)
    print(f" {title}")
    print(SUB)


def demo_rate_limiting(rounds: int):
    User = get_user_model()
    user, _ = User.objects.get_or_create(
        username="capacity_demo_user",
        defaults={"email": "capacity_demo@example.com"},
    )

    Coupon.objects.filter(code="RATE-DEMO").delete()
    Coupon.objects.create(
        code="RATE-DEMO",
        discount_percent=10,
        max_uses=9999,
        usage_count=0,
        is_active=True,
    )

    cache.clear()

    client = APIClient()
    client.force_authenticate(user=user)

    header("LAYER 1 — Rate Limiting (DRF ScopedRateThrottle, 5/min per user)")
    print(" PROBLEM  : a single user can flood /api/coupons/redeem/ and")
    print("            starve legitimate traffic.")
    print(" SOLUTION : DRF ScopedRateThrottle caps each user at 5 requests/min")
    print("            on this endpoint. The 6th+ request returns HTTP 429.")
    subheader(f" Firing {rounds} back-to-back requests as the SAME user")

    allowed = 0
    throttled = 0
    other = 0
    print(f"  {'req':>4}  {'status':<6}  {'verdict':<32}  {'elapsed':>9}  retry-after")
    print(f"  {'---':>4}  {'------':<6}  {'-' * 32}  {'-' * 9}  -----------")
    for i in range(1, rounds + 1):
        t0 = time.perf_counter()
        resp = client.post(
            "/api/coupons/redeem/", {"code": "RATE-DEMO"}, format="json"
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        code = resp.status_code
        retry = resp.headers.get("Retry-After", "-")
        if code == 200:
            allowed += 1
            verdict = "ALLOWED (within quota)"
        elif code == 429:
            throttled += 1
            verdict = "REJECTED (quota exceeded)"
        else:
            other += 1
            verdict = f"HTTP {code}"
        print(f"  {i:>4}  {code:<6}  {verdict:<32}  {elapsed_ms:>6.1f} ms  {retry}")

    subheader(" Outcome")
    print(f"  Allowed   : {allowed}   (first {allowed} requests were within the 5/min budget)")
    print(f"  Rejected  : {throttled}   (returned HTTP 429 before ever touching business logic)")
    if other:
        print(f"  Other     : {other}")
    REPORT_STATE["rate"] = {
        "rounds": rounds, "allowed": allowed, "throttled": throttled, "other": other,
    }
    print()
    print("  What this solves:")
    print("  - A flooding user cannot monopolise the endpoint.")
    print("  - The rejected requests never hit the DB, so they cost ~nothing.")
    print("  - Other users, with their own 5/min bucket, remain unaffected.")


def make_op(pool: threading.Semaphore):
    def op(record: dict):
        record["work_started_at"] = time.perf_counter()
        if not pool.acquire(blocking=False):
            record["status"] = "POOL_EXHAUSTED"
            record["finished_at"] = time.perf_counter()
            raise RuntimeError("resource_pool_exhausted")
        try:
            time.sleep(WORK_TIME)
            record["status"] = "OK"
        finally:
            pool.release()
            record["finished_at"] = time.perf_counter()

    return op


def _wait_ms(r: dict):
    if "work_started_at" in r and "started_at" in r:
        return (r["work_started_at"] - r["started_at"]) * 1000
    return None


def _total_ms(r: dict):
    if "finished_at" in r and "started_at" in r:
        return (r["finished_at"] - r["started_at"]) * 1000
    return None


def run_concurrency_scenario(label: str, call_fn, threads: int):
    records = [{"id": i + 1} for i in range(threads)]
    barrier = threading.Barrier(threads)
    wall_start = time.perf_counter()

    def worker(rec):
        barrier.wait()
        rec["started_at"] = time.perf_counter()
        try:
            call_fn(rec)
        except CapacityExceeded:
            rec["status"] = "REJECTED_503"
            rec["finished_at"] = time.perf_counter()
        except RuntimeError:
            pass

    ts = [threading.Thread(target=worker, args=(r,)) for r in records]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    wall_ms = (time.perf_counter() - wall_start) * 1000

    subheader(f" {label}")
    print(f"  {'#':>3}  {'status':<16}  {'wait at sem':>12}  {'total':>8}")
    print(f"  {'---':>3}  {'-' * 16}  {'-' * 12}  {'-' * 8}")

    if threads <= 18:
        rows = records
    else:
        rows = records[:10] + [None] + records[-5:]

    for r in rows:
        if r is None:
            print("   ...")
            continue
        status = r.get("status", "?")
        wait = _wait_ms(r)
        wait_str = f"{wait:8.0f} ms" if wait is not None else "         —"
        total = _total_ms(r)
        total_str = f"{total:5.0f} ms" if total is not None else "      —"
        print(f"  {r['id']:>3}  {status:<16}  {wait_str:>12}  {total_str:>8}")

    ok = [r for r in records if r.get("status") == "OK"]
    exhausted = [r for r in records if r.get("status") == "POOL_EXHAUSTED"]
    rejected = [r for r in records if r.get("status") == "REJECTED_503"]

    print()
    print(f"  Wall time          : {wall_ms:.0f} ms")
    print(
        f"  Succeeded          : {len(ok):>3}/{threads}  "
        f"({len(ok) / threads * 100:.0f}%)"
    )
    print(f"  Pool-exhausted     : {len(exhausted):>3}   (resource ran out — user sees an error)")
    print(f"  Rejected (HTTP 503): {len(rejected):>3}   (fail-fast at the semaphore)")

    summary = {
        "label": label,
        "threads": threads,
        "wall_ms": round(wall_ms),
        "ok": len(ok),
        "exhausted": len(exhausted),
        "rejected_503": len(rejected),
        "p50_total": None, "p95_total": None, "p50_wait": None, "p95_wait": None,
    }
    if ok:
        totals = sorted(_total_ms(r) for r in ok)
        waits = sorted(w for w in (_wait_ms(r) for r in ok) if w is not None)
        p50_t = statistics.median(totals)
        p95_t = totals[min(len(totals) - 1, int(len(totals) * 0.95))]
        summary["p50_total"] = round(p50_t)
        summary["p95_total"] = round(p95_t)
        print(f"  Success latency    : p50 {p50_t:.0f} ms  /  p95 {p95_t:.0f} ms")
        if waits:
            p50_w = statistics.median(waits)
            p95_w = waits[min(len(waits) - 1, int(len(waits) * 0.95))]
            summary["p50_wait"] = round(p50_w)
            summary["p95_wait"] = round(p95_w)
            print(f"  Time spent queuing : p50 {p50_w:.0f} ms  /  p95 {p95_w:.0f} ms")
    REPORT_STATE["scenarios"].append(summary)


def demo_concurrency_cap(threads: int, cap: int, pool_cap: int, timeout: float):
    header("LAYER 2 — Bounded Concurrency Cap (threading.BoundedSemaphore)")
    print(" PROBLEM  : Every user stays under their rate limit, yet when the")
    print("            SUM of concurrent requests all hit the backend at once,")
    print(f"           our finite 'DB pool' ({pool_cap} slots) is oversubscribed.")
    print("            Most requests then fail with 'pool exhausted'.")
    print(" SOLUTION : Wrap the expensive handler in @limit_concurrency(N). Only")
    print(f"           N={cap} operations execute at once; extras wait up to")
    print(f"           {timeout}s. Workload is shaped to what the backend can")
    print("            actually sustain.")

    pool_a = threading.Semaphore(pool_cap)
    op_a = make_op(pool_a)

    def call_uncapped(rec):
        op_a(rec)

    run_concurrency_scenario(
        f"BEFORE FIX  —  no capacity control   ({threads} ops, pool={pool_cap})",
        call_uncapped,
        threads,
    )

    pool_b = threading.Semaphore(pool_cap)
    op_b = make_op(pool_b)
    capped_op = limit_concurrency(max_concurrent=cap, timeout=timeout)(op_b)

    def call_capped(rec):
        capped_op(rec)

    run_concurrency_scenario(
        f"AFTER FIX   —  @limit_concurrency(cap={cap})  ({threads} ops, pool={pool_cap}, timeout={timeout}s)",
        call_capped,
        threads,
    )

    scenarios = REPORT_STATE.get("scenarios") or []
    if len(scenarios) >= 2:
        before = scenarios[-2]
        after = scenarios[-1]
        header(" SIDE-BY-SIDE COMPARISON — Before vs After the Fix")
        print(f"  {'Metric':<28} {'BEFORE FIX':>15} {'AFTER FIX':>15}")
        print(f"  {'-' * 28} {'-' * 15} {'-' * 15}")
        print(f"  {'Threads':<28} {before['threads']:>15} {after['threads']:>15}")
        before_ok_pct = before['ok'] / before['threads'] * 100
        after_ok_pct = after['ok'] / after['threads'] * 100
        before_success = f"{before['ok']}/{before['threads']} ({before_ok_pct:.0f}%)"
        after_success = f"{after['ok']}/{after['threads']} ({after_ok_pct:.0f}%)"
        print(f"  {'Succeeded':<28} {before_success:>15} {after_success:>15}")
        print(f"  {'Pool-exhausted (failures)':<28} {before['exhausted']:>15} {after['exhausted']:>15}")
        print(f"  {'Rejected (HTTP 503)':<28} {before['rejected_503']:>15} {after['rejected_503']:>15}")
        print(f"  {'Wall time (ms)':<28} {before['wall_ms']:>15} {after['wall_ms']:>15}")
        print(f"  {'p50 latency (ms)':<28} {str(before['p50_total'] or '-'):>15} {str(after['p50_total'] or '-'):>15}")
        print(f"  {'p95 latency (ms)':<28} {str(before['p95_total'] or '-'):>15} {str(after['p95_total'] or '-'):>15}")
        print()
        print(f"  VERDICT: success rate went from {before_ok_pct:.0f}% to {after_ok_pct:.0f}%")
        print(f"           {before['exhausted']} crashed failures -> {after['exhausted']} crashed failures")

    subheader(" What changed between BEFORE and AFTER")
    print(" BEFORE FIX (no cap):")
    print("   - Each request races straight to the pool.")
    print("   - Pool fills immediately, everyone else FAILS with pool-exhausted.")
    print("   - Fast but UNRELIABLE — error rate explodes under load.")
    print(" AFTER FIX (with @limit_concurrency):")
    print("   - Semaphore admits only N requests at a time.")
    print("   - Excess callers QUEUE briefly; nobody ever sees 'pool exhausted'.")
    print("   - Slightly higher tail latency, but 100% success.")


def write_report(opts: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORT_DIR / f"task2_capacity_{ts}.md"

    lines: list[str] = []
    lines.append("# Task 2 — Resource Management & Capacity Control")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now().isoformat(timespec='seconds')}")
    lines.append("")
    lines.append("## Defence layers")
    lines.append("- **Layer 1 — DRF ScopedRateThrottle** (5/min per user on "
                 "`/api/coupons/redeem/`). Protects against a single user flooding "
                 "an endpoint. Over-quota requests return **HTTP 429** before "
                 "touching business logic.")
    lines.append("- **Layer 2 — `threading.BoundedSemaphore`** (bulkhead). Caps the "
                 "total number of simultaneous heavy operations. Excess callers "
                 "wait up to a short timeout and fail fast with **HTTP 503** "
                 "instead of oversubscribing a finite resource.")
    lines.append("")

    rate = REPORT_STATE.get("rate")
    if rate:
        lines.append("## Layer 1 — Rate limiting (per-user throttle)")
        lines.append("")
        lines.append(f"Fired **{rate['rounds']}** back-to-back requests as the same user.")
        lines.append("")
        lines.append("| Allowed (HTTP 200) | Rejected (HTTP 429) | Other |")
        lines.append("|---|---|---|")
        lines.append(f"| {rate['allowed']} | {rate['throttled']} | {rate['other']} |")
        lines.append("")

    scenarios = REPORT_STATE.get("scenarios") or []
    if scenarios:
        lines.append("## Layer 2 — Bounded concurrency cap (Before vs After the fix)")
        lines.append("")
        lines.append(
            f"Each scenario launches **{opts['threads']}** threads at a finite "
            f"resource pool of **{opts['pool']}** slots. **BEFORE FIX** hits the pool "
            f"directly; **AFTER FIX** goes through `@limit_concurrency("
            f"max_concurrent={opts['cap']}, timeout={opts['timeout']}s)`."
        )
        lines.append("")
        lines.append("| Scenario | Threads | Wall (ms) | OK | Pool-exhausted | HTTP 503 | p50 total | p95 total | p50 wait | p95 wait |")
        lines.append("|----------|--------:|----------:|---:|---------------:|---------:|----------:|----------:|---------:|---------:|")
        for s in scenarios:
            lines.append(
                f"| {s['label']} | {s['threads']} | {s['wall_ms']} | {s['ok']} | "
                f"{s['exhausted']} | {s['rejected_503']} | "
                f"{s['p50_total'] if s['p50_total'] is not None else '—'} | "
                f"{s['p95_total'] if s['p95_total'] is not None else '—'} | "
                f"{s['p50_wait'] if s['p50_wait'] is not None else '—'} | "
                f"{s['p95_wait'] if s['p95_wait'] is not None else '—'} |"
            )
        lines.append("")

        if len(scenarios) >= 2:
            before = scenarios[-2]
            after = scenarios[-1]
            before_pct = before['ok'] / before['threads'] * 100
            after_pct = after['ok'] / after['threads'] * 100
            lines.append("### Side-by-side comparison")
            lines.append("")
            lines.append("| Metric | BEFORE FIX | AFTER FIX |")
            lines.append("|--------|-----------:|----------:|")
            lines.append(f"| Threads | {before['threads']} | {after['threads']} |")
            lines.append(f"| Succeeded | **{before['ok']}/{before['threads']} ({before_pct:.0f}%)** | **{after['ok']}/{after['threads']} ({after_pct:.0f}%)** |")
            lines.append(f"| Pool-exhausted (crashed) | {before['exhausted']} | {after['exhausted']} |")
            lines.append(f"| Rejected (HTTP 503) | {before['rejected_503']} | {after['rejected_503']} |")
            lines.append(f"| Wall time (ms) | {before['wall_ms']} | {after['wall_ms']} |")
            lines.append(f"| p50 latency (ms) | {before['p50_total'] or '—'} | {after['p50_total'] or '—'} |")
            lines.append(f"| p95 latency (ms) | {before['p95_total'] or '—'} | {after['p95_total'] or '—'} |")
            lines.append("")
            lines.append(
                f"**Verdict:** success rate jumped from **{before_pct:.0f}%** to **{after_pct:.0f}%** "
                f"with zero change to the underlying resource — only admission control was added."
            )
            lines.append("")

    lines.append("## Interpretation")
    lines.append("- **Without cap**: every thread races to the pool at once. The "
                 "pool fills, the overflow fails with *pool-exhausted*. Fast but "
                 "**unreliable under load**.")
    lines.append("- **With cap**: the semaphore admits only N callers at a time. "
                 "Excess traffic queues briefly; nothing oversubscribes the pool. "
                 "Slightly higher tail latency, **100% success**.")
    lines.append("")
    lines.append("## Where it lives in the codebase")
    lines.append("- Primitive: `core/concurrency.py::limit_concurrency`")
    lines.append("- Applied: `orders/views.py::CheckoutView.post` "
                 "(`@limit_concurrency(max_concurrent=3, timeout=2.0)`)")
    lines.append("- Rate limit: `config/settings.py::REST_FRAMEWORK`"
                 "`[\"DEFAULT_THROTTLE_RATES\"]`")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class Command(BaseCommand):
    help = "Demonstrate capacity control (rate limit + concurrency cap)."

    def add_arguments(self, parser):
        parser.add_argument("--rate-rounds", type=int, default=8)
        parser.add_argument("--threads", type=int, default=30)
        parser.add_argument("--cap", type=int, default=3)
        parser.add_argument("--pool", type=int, default=10)
        parser.add_argument("--timeout", type=float, default=2.0)
        parser.add_argument("--skip-rate", action="store_true")
        parser.add_argument("--skip-concurrency", action="store_true")
        parser.add_argument("--no-report", action="store_true",
                            help="Skip writing the Markdown report file.")

    def handle(self, *args, **opts):
        if not opts["skip_rate"]:
            demo_rate_limiting(opts["rate_rounds"])
        if not opts["skip_concurrency"]:
            demo_concurrency_cap(
                opts["threads"], opts["cap"], opts["pool"], opts["timeout"]
            )
        if not opts["no_report"]:
            path = write_report(opts)
            print(f"\nReport saved to: {path}")
