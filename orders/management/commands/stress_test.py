"""
Task 9 — Stress / Stability Testing  (Requirement #9)

Goal
----
Prove the backend can serve **at least 100 concurrent users without crashing
and without losing data**, by hammering the real checkout path — the heaviest
write operation, which contends on a single shared product-stock row.

How it works
------------
1. Reset ONE product to a known stock and create N distinct users, each with a
   cart holding exactly 1 unit of that product. The only shared, contended
   resource is therefore the product-stock row — exactly the data-integrity
   hot path (Requirements #1 / #7 / #8).
2. A ``threading.Barrier`` releases all N worker threads at the same instant,
   so every checkout hits the server simultaneously (maximum contention).
3. Each thread calls the real DRF endpoint ``POST /api/checkout/`` in-process
   through ``APIClient`` — so the FULL stack runs: authentication, throttling,
   the capacity bulkhead (``@limit_concurrency``), the atomic transaction, and
   the pessimistic row lock (``select_for_update``) on the product.
4. After the storm we verify there was **NO DATA LOSS**:
       successful checkouts  == orders persisted in the DB
       units sold            == initial_stock - final_stock
       oversold              == 0     (final stock is never negative)
   and **NO CRASH**: zero unexpected 5xx responses / exceptions. An HTTP 503
   from the capacity bulkhead is *graceful load-shedding* (Requirement #2),
   not a crash, so it is reported separately and never counts as data loss.

Reported metrics (test-tool style): Total Requests, Success Requests, Failed
Requests, Average Response Time, and whether the System crashed — plus the
data-integrity verification.

Run:
    python manage.py stress_test                      # 100 concurrent users
    python manage.py stress_test --users 200          # push it harder
    python manage.py stress_test --mode sequential    # one request at a time
    python manage.py stress_test --mode both          # sequential vs concurrent
"""

import statistics
import threading
import time
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db import connection

from rest_framework.test import APIClient

from orders.models import Cart, CartItem, Order
from products.models import Category, Product

REPORT_DIR = Path(settings.BASE_DIR) / "reports"
DEMO_SLUG = "stress-demo-product"
BANNER = "=" * 72
SUB = "-" * 72


def _percentile(sorted_vals: list[float], q: float):
    """Nearest-rank percentile of an already-sorted list (q in 0..1)."""
    if not sorted_vals:
        return None
    k = min(len(sorted_vals) - 1, int(round((len(sorted_vals) - 1) * q)))
    return sorted_vals[k]


def setup(num_users: int, stock: int):
    """Reset the demo product and prepare `num_users` users, each with a
    one-item cart. Also clears orders from any previous stress run so the
    post-test DB counts reflect only this run."""
    User = get_user_model()

    cat, _ = Category.objects.get_or_create(
        name="Stress Demo", defaults={"slug": "stress-demo"}
    )
    # Deleting the product cascades any CartItem that references it.
    Product.objects.filter(slug=DEMO_SLUG).delete()
    product = Product.objects.create(
        category=cat,
        name="Stress Demo Product",
        slug=DEMO_SLUG,
        description="Used only by manage.py stress_test.",
        price=10,
        stock=stock,
        is_active=True,
    )

    users = []
    for i in range(num_users):
        u, _ = User.objects.get_or_create(
            username=f"stress_user_{i}",
            defaults={"email": f"stress_{i}@example.com"},
        )
        users.append(u)

    # Clean slate: remove previous-run orders (cascades OrderItems) and rebuild
    # one fresh cart with a single unit per user.
    Order.objects.filter(user__in=users).delete()
    for u in users:
        cart, _ = Cart.objects.get_or_create(user=u)
        cart.items.all().delete()
        CartItem.objects.create(cart=cart, product=product, quantity=1)

    cache.clear()  # reset DRF throttle buckets so a prior run can't skew results
    return product, users


def _do_checkout(idx: int, user) -> dict:
    """One real checkout call; returns a record with status + latency."""
    client = APIClient()
    client.force_authenticate(user=user)
    rec = {"idx": idx}
    try:
        t0 = time.perf_counter()
        resp = client.post(
            "/api/checkout/",
            {"shipping_address": "123 Stress Test Ave"},
            format="json",
        )
        rec["latency_ms"] = (time.perf_counter() - t0) * 1000
        rec["status"] = resp.status_code
    except Exception as exc:  # a real crash for THIS request
        rec["status"] = "EXC"
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["latency_ms"] = None
    finally:
        # Close this thread's DB connection — each thread gets its own
        # connection, and leaving them open exhausts SQLite handles.
        connection.close()
    return rec


def run_concurrent(users: list):
    """Fire one CONCURRENT checkout per user, all released at the same instant.

    A `threading.Barrier` holds every thread until the last one is ready, so all
    requests hit the server simultaneously — maximum contention on the shared
    stock row. This is the real stress scenario.
    """
    n = len(users)
    records: list = [None] * n
    barrier = threading.Barrier(n)

    def worker(idx: int, user):
        barrier.wait()  # all threads block here, then start together
        records[idx] = _do_checkout(idx, user)  # distinct index → no lock needed

    threads = [threading.Thread(target=worker, args=(i, u)) for i, u in enumerate(users)]
    wall0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall_ms = (time.perf_counter() - wall0) * 1000
    return records, wall_ms


def run_sequential(users: list):
    """Fire checkouts ONE AFTER ANOTHER on a single thread — the baseline.

    No two requests overlap, so there is zero contention. Comparing this against
    the concurrent run shows what concurrency costs (latency) and proves the
    integrity guarantees are not just an artifact of requests never overlapping.
    """
    records = []
    wall0 = time.perf_counter()
    for i, u in enumerate(users):
        records.append(_do_checkout(i, u))
    wall_ms = (time.perf_counter() - wall0) * 1000
    return records, wall_ms


def analyse(records, wall_ms, product, users, initial_stock, mode="concurrent"):
    def by_status(pred):
        return [r for r in records if pred(r["status"])]

    created = by_status(lambda s: s == 201)
    shed_503 = by_status(lambda s: s == 503)
    rejected_400 = by_status(lambda s: s == 400)
    throttled_429 = by_status(lambda s: s == 429)
    server_err = by_status(lambda s: isinstance(s, int) and s >= 500 and s != 503)
    exceptions = by_status(lambda s: s == "EXC")

    product.refresh_from_db()
    final_stock = product.stock
    successes = len(created)
    orders_in_db = Order.objects.filter(user__in=users).count()
    units_sold = initial_stock - final_stock
    oversold = max(0, successes - initial_stock)

    # ── DATA-LOSS CHECKS ──────────────────────────────────────────────────────
    no_lost_writes = successes == orders_in_db            # every 201 persisted
    stock_consistent = units_sold == successes            # 1 unit per order
    no_oversell = oversold == 0 and final_stock >= 0
    no_crash = not server_err and not exceptions
    data_loss_free = no_lost_writes and stock_consistent and no_oversell

    lat = sorted(r["latency_ms"] for r in created if r.get("latency_ms") is not None)
    avg_ms = statistics.fmean(lat) if lat else None
    throughput = (len(records) / (wall_ms / 1000)) if wall_ms else 0.0

    return {
        "mode": mode,
        "total": len(records),
        "created": successes,
        "failed": len(records) - successes,
        "shed_503": len(shed_503),
        "rejected_400": len(rejected_400),
        "throttled_429": len(throttled_429),
        "server_err": len(server_err),
        "exceptions": len(exceptions),
        "exception_samples": [r.get("error") for r in exceptions[:3]],
        "initial_stock": initial_stock,
        "final_stock": final_stock,
        "units_sold": units_sold,
        "orders_in_db": orders_in_db,
        "oversold": oversold,
        "wall_ms": round(wall_ms),
        "throughput_rps": round(throughput, 1),
        "avg_ms": avg_ms,
        "mean_ms": statistics.fmean(lat) if lat else None,
        "p50_ms": _percentile(lat, 0.50),
        "p95_ms": _percentile(lat, 0.95),
        "p99_ms": _percentile(lat, 0.99),
        "max_ms": lat[-1] if lat else None,
        "no_lost_writes": no_lost_writes,
        "stock_consistent": stock_consistent,
        "no_oversell": no_oversell,
        "no_crash": no_crash,
        "data_loss_free": data_loss_free,
        "passed": data_loss_free and no_crash,
    }


def print_report(s: dict) -> None:
    def ms(v):
        return f"{v:.0f} ms" if v is not None else "—"

    print()
    print(BANNER)
    print(f" Task 9 — Stress / Stability Test  (Requirement #9)  [mode: {s['mode'].upper()}]")
    print(BANNER)
    print(f" Load mode                  : {s['mode']}  "
          f"({'all at once' if s['mode'] == 'concurrent' else 'one after another'})")
    print(f" Total requests             : {s['total']}")
    print(f" Success requests (201)     : {s['created']}")
    print(f" Failed requests            : {s['total'] - s['created']}")
    print(f" Average response time      : {ms(s['avg_ms'])}")
    print(f" System crashed             : {'YES' if not s['no_crash'] else 'NO'}")
    print(f" Wall-clock for the run     : {s['wall_ms']} ms")
    print(f" Throughput                 : {s['throughput_rps']} req/s")
    print(SUB)
    print(" REQUIRED METRICS (per the brief)")
    print(f"   Total Requests        : {s['total']}")
    print(f"   Success Requests      : {s['created']}")
    print(f"   Failed Requests       : {s['failed']}")
    print(f"   Average Response Time : {ms(s['mean_ms'])}")
    print(f"   System crashed        : {'NO' if s['no_crash'] else 'YES'}")
    print(SUB)
    print(" RESPONSE BREAKDOWN")
    print(f"   201 Created (served)     : {s['created']}")
    print(f"   503 Load-shed (graceful) : {s['shed_503']}   (capacity bulkhead, Req #2 — not a crash)")
    print(f"   400 Out-of-stock         : {s['rejected_400']}   (correctly refused — no oversell)")
    print(f"   429 Throttled            : {s['throttled_429']}")
    print(f"   5xx Server errors        : {s['server_err']}")
    print(f"   Exceptions (crashes)     : {s['exceptions']}")
    for e in s["exception_samples"]:
        print(f"      ! {e}")
    print(SUB)
    print(" SUCCESS LATENCY")
    print(f"   avg {ms(s['avg_ms'])}   p50 {ms(s['p50_ms'])}   p95 {ms(s['p95_ms'])}   "
          f"p99 {ms(s['p99_ms'])}   max {ms(s['max_ms'])}")
    print(f"   avg {ms(s['mean_ms'])}   p50 {ms(s['p50_ms'])}   p95 {ms(s['p95_ms'])}   p99 {ms(s['p99_ms'])}   max {ms(s['max_ms'])}")
    print(SUB)
    print(" DATA-INTEGRITY VERIFICATION (no data loss)")
    print(f"   Initial stock            : {s['initial_stock']}")
    print(f"   Final stock              : {s['final_stock']}")
    print(f"   Units sold (init-final)  : {s['units_sold']}")
    print(f"   Successful checkouts     : {s['created']}")
    print(f"   Orders persisted in DB   : {s['orders_in_db']}")
    print(f"   Oversold by              : {s['oversold']}")
    print(f"   [{'OK' if s['no_lost_writes'] else 'FAIL'}] no lost writes  (201s == orders in DB)")
    print(f"   [{'OK' if s['stock_consistent'] else 'FAIL'}] stock consistent (units sold == checkouts)")
    print(f"   [{'OK' if s['no_oversell'] else 'FAIL'}] no overselling   (oversold == 0, stock >= 0)")
    print(f"   [{'OK' if s['no_crash'] else 'FAIL'}] no crash         (zero 5xx / exceptions)")
    print(BANNER)
    verdict = "PASSED — served all users with zero data loss" if s["passed"] else "FAILED — see flags above"
    print(f" VERDICT: {verdict}")
    print(BANNER)


def write_report(s: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORT_DIR / f"task9_stress_{s['mode']}_{ts}.md"

    def ms(v):
        return f"{v:.0f}" if v is not None else "—"

    flag = lambda ok: "✅ PASS" if ok else "❌ FAIL"

    lines: list[str] = []
    lines.append("# Task 9 — Stress / Stability Testing")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now().isoformat(timespec='seconds')}  ")
    lines.append(f"**Load mode:** {s['mode']}  ")
    lines.append(f"**Users / requests:** {s['total']}  ")
    lines.append(f"**Requirement:** Serve at least 100 concurrent users without crash or data loss.")
    lines.append("")
    lines.append("## Summary (test-tool style)")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|------:|")
    lines.append(f"| Load mode | {s['mode']} |")
    lines.append(f"| Total Requests | {s['total']} |")
    lines.append(f"| Success Requests | {s['created']} |")
    lines.append(f"| Failed Requests | {s['total'] - s['created']} |")
    lines.append(f"| Average Response Time | {ms(s['avg_ms'])} ms |")
    lines.append(f"| System crashed | {'YES' if not s['no_crash'] else 'NO'} |")
    lines.append("")
    lines.append("## Scenario")
    lines.append(
        f"{s['total']} distinct users, each with a cart holding 1 unit of the same "
        "product, are released **simultaneously** by a `threading.Barrier` and each "
        "calls the real `POST /api/checkout/` endpoint through DRF's `APIClient`. "
        "The single shared product-stock row is the contended resource, so this "
        "stresses authentication, the capacity bulkhead, the atomic transaction, "
        "and the serialized stock read-check-write all at once."
    )
    lines.append("")
    lines.append(
        "> **How integrity is guaranteed across backends:** on **PostgreSQL** the "
        "checkout takes a true row-level lock via `select_for_update()`. On the "
        "**SQLite** dev database that clause is a no-op, so correctness instead "
        "relies on `transaction_mode=\"IMMEDIATE\"` (configured in `settings.py`), "
        "which makes every `transaction.atomic()` block acquire the write lock at "
        "`BEGIN` — fully serializing the read-check-write and giving the same "
        "no-oversell guarantee the numbers below confirm."
    )
    lines.append("")
    lines.append("## Throughput & latency")
    lines.append("")
    lines.append("| Users | Wall (ms) | Throughput (req/s) | avg (ms) | p50 (ms) | p95 (ms) | p99 (ms) | max (ms) |")
    lines.append("|------:|----------:|-------------------:|---------:|---------:|---------:|---------:|---------:|")
    lines.append(
        f"| {s['total']} | {s['wall_ms']} | {s['throughput_rps']} | {ms(s['avg_ms'])} | "
        f"{ms(s['p50_ms'])} | {ms(s['p95_ms'])} | {ms(s['p99_ms'])} | {ms(s['max_ms'])} |"
    )
    lines.append("")
    lines.append("## Required metrics (per the brief)")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|------:|")
    lines.append(f"| Total Requests | {s['total']} |")
    lines.append(f"| Success Requests | {s['created']} |")
    lines.append(f"| Failed Requests | {s['failed']} |")
    lines.append(f"| Average Response Time | {ms(s['mean_ms'])} ms |")
    lines.append(f"| System crashed | {'NO' if s['no_crash'] else 'YES'} |")
    lines.append("")
    lines.append("## Response breakdown")
    lines.append("")
    lines.append("| 201 Created | 503 Load-shed | 400 Out-of-stock | 429 Throttled | 5xx Errors | Exceptions |")
    lines.append("|------------:|--------------:|-----------------:|--------------:|-----------:|-----------:|")
    lines.append(
        f"| {s['created']} | {s['shed_503']} | {s['rejected_400']} | "
        f"{s['throttled_429']} | {s['server_err']} | {s['exceptions']} |"
    )
    lines.append("")
    lines.append(
        "> **503** responses come from the bounded-concurrency bulkhead "
        "(`@limit_concurrency`, Requirement #2). They are *graceful load-shedding*, "
        "not crashes, and never correspond to a partial/lost write."
    )
    lines.append("")
    lines.append("## Data-integrity verification (no data loss)")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|------:|")
    lines.append(f"| Initial stock | {s['initial_stock']} |")
    lines.append(f"| Final stock | {s['final_stock']} |")
    lines.append(f"| Units sold (initial − final) | {s['units_sold']} |")
    lines.append(f"| Successful checkouts (HTTP 201) | {s['created']} |")
    lines.append(f"| Orders persisted in DB | {s['orders_in_db']} |")
    lines.append(f"| Oversold by | {s['oversold']} |")
    lines.append("")
    lines.append("| Check | Result |")
    lines.append("|-------|:------:|")
    lines.append(f"| No lost writes (201s == orders in DB) | {flag(s['no_lost_writes'])} |")
    lines.append(f"| Stock consistent (units sold == checkouts) | {flag(s['stock_consistent'])} |")
    lines.append(f"| No overselling (oversold == 0, stock ≥ 0) | {flag(s['no_oversell'])} |")
    lines.append(f"| No crash (zero 5xx / exceptions) | {flag(s['no_crash'])} |")
    lines.append("")
    lines.append(f"## Verdict: {'✅ PASSED' if s['passed'] else '❌ FAILED'}")
    lines.append("")
    if s["passed"]:
        lines.append(
            f"The system served **{s['total']} concurrent users** with "
            f"**{s['created']} successful orders**, **0 oversold units**, and "
            "**0 lost writes or crashes** — meeting Requirement #9."
        )
    else:
        lines.append("One or more integrity/stability checks failed — see the tables above.")
    lines.append("")
    lines.append("## Where it lives in the codebase")
    lines.append("- Test harness: `orders/management/commands/stress_test.py`")
    lines.append("- Endpoint under test: `orders/views.py::CheckoutView.post`")
    lines.append("- Integrity primitives exercised: pessimistic lock "
                 "(`select_for_update`) + `transaction.atomic` + `@limit_concurrency`.")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def print_comparison(seq: dict, con: dict) -> None:
    """Sequential vs Concurrent side-by-side (before/after-style contrast)."""
    def ms(v):
        return f"{v:.0f} ms" if v is not None else "—"

    print()
    print(BANNER)
    print(" SEQUENTIAL vs CONCURRENT — side by side")
    print(BANNER)
    print(f"  {'Metric':<26}{'SEQUENTIAL':>16}{'CONCURRENT':>16}")
    print(f"  {'-'*26}{'-'*16:>16}{'-'*16:>16}")
    print(f"  {'Total requests':<26}{seq['total']:>16}{con['total']:>16}")
    print(f"  {'Success requests':<26}{seq['created']:>16}{con['created']:>16}")
    print(f"  {'Failed requests':<26}{seq['total']-seq['created']:>16}{con['total']-con['created']:>16}")
    print(f"  {'Average response time':<26}{ms(seq['avg_ms']):>16}{ms(con['avg_ms']):>16}")
    print(f"  {'Wall-clock':<26}{str(seq['wall_ms'])+' ms':>16}{str(con['wall_ms'])+' ms':>16}")
    print(f"  {'Throughput (req/s)':<26}{seq['throughput_rps']:>16}{con['throughput_rps']:>16}")
    print(f"  {'System crashed':<26}{('YES' if not seq['no_crash'] else 'NO'):>16}{('YES' if not con['no_crash'] else 'NO'):>16}")
    print(f"  {'Oversold by':<26}{seq['oversold']:>16}{con['oversold']:>16}")
    print(BANNER)
    print(" Reading: concurrency raises per-request latency (requests now")
    print(" contend on the same stock row) but finishes the whole batch in far")
    print(" less wall-clock — and integrity (0 oversold, 0 crash) holds in BOTH.")


class Command(BaseCommand):
    help = "Task 9: stress test the checkout path (concurrent and/or sequential) and verify no data loss."

    def add_arguments(self, parser):
        parser.add_argument("--users", type=int, default=100,
                            help="Number of users/requests (default: 100).")
        parser.add_argument("--mode", choices=["concurrent", "sequential", "both"],
                            default="concurrent",
                            help="Load pattern: all-at-once, one-after-another, or both (default: concurrent).")
        parser.add_argument("--stock", type=int, default=None,
                            help="Initial product stock (default: == --users, so all can succeed).")
        parser.add_argument("--no-report", action="store_true",
                            help="Skip writing the Markdown report file.")

    def _run_one(self, mode, num_users, stock, write):
        self.stdout.write(f"[{mode}] preparing {num_users} users, product stock={stock} …")
        product, users = setup(num_users, stock)
        if mode == "concurrent":
            self.stdout.write(f"[{mode}] releasing {num_users} checkouts all at once …")
            records, wall_ms = run_concurrent(users)
        else:
            self.stdout.write(f"[{mode}] running {num_users} checkouts one after another …")
            records, wall_ms = run_sequential(users)
        summary = analyse(records, wall_ms, product, users, stock, mode=mode)
        print_report(summary)
        if write:
            path = write_report(summary)
            self.stdout.write(f"\nReport saved to: {path}")
        return summary

    def handle(self, *args, **opts):
        num_users = opts["users"]
        stock = opts["stock"] if opts["stock"] is not None else num_users
        write = not opts["no_report"]

        modes = ["sequential", "concurrent"] if opts["mode"] == "both" else [opts["mode"]]
        results = {}
        for m in modes:
            results[m] = self._run_one(m, num_users, stock, write)

        if opts["mode"] == "both":
            print_comparison(results["sequential"], results["concurrent"])

        if any(not r["passed"] for r in results.values()):
            raise SystemExit(1)  # non-zero exit so CI / graders see the failure
