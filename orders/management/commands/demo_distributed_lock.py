"""Requirement #7 demo — Concurrency Control with a DISTRIBUTED lock.

Reproduces a cross-process lost-update race on product stock, then fixes it with
a Redis distributed lock, and prints a BEFORE vs AFTER comparison + a report,
matching the style of demo_stock_race / demo_capacity.

    python manage.py demo_distributed_lock                 # both, 50 workers
    python manage.py demo_distributed_lock --workers 100
    python manage.py demo_distributed_lock --mode safe

Start real Redis first for a TRUE distributed lock (otherwise it falls back to
an in-process lock and the report says so):

    docker run --rm -p 6379:6379 redis          # or run Memurai on Windows
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

from core.aop import perf
from core.distributed_lock import active_backend, reset_redis_state, BACKEND_REDIS
from orders.services import (
    decrement_stock_dlock_safe,
    decrement_stock_dlock_unsafe,
)
from products.models import Category, Product

DEMO_SLUG = "dlock-demo-product"
REPORT_DIR = Path(settings.BASE_DIR) / "reports"
BANNER = "=" * 70


def reset_product(stock: int) -> Product:
    cat, _ = Category.objects.get_or_create(name="Demo", defaults={"slug": "demo"})
    Product.objects.filter(slug=DEMO_SLUG).delete()
    return Product.objects.create(
        category=cat, name="DLock Demo Product", slug=DEMO_SLUG,
        description="Used only by manage.py demo_distributed_lock.",
        price=100, stock=stock, is_active=True,
    )


def run_scenario(mode: str, workers: int, stock: int) -> dict:
    product = reset_product(stock)
    fn = decrement_stock_dlock_safe if mode == "safe" else decrement_stock_dlock_unsafe
    perf.reset()

    successes: list[int] = []
    failures: list[tuple[int, str]] = []
    errors: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(workers)

    def worker(i: int):
        # Each thread stands in for a request landing on one of several servers.
        try:
            barrier.wait()                     # release all at once → max contention
            ok, reason = fn(product.pk, 1)
            with lock:
                (successes if ok else failures).append(i if ok else (i, reason))
        except Exception as exc:
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            connection.close()

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    product.refresh_from_db()
    oversold = max(0, len(successes) - stock)
    op = "stock.dlock_safe" if mode == "safe" else "stock.dlock_unsafe"

    return {
        "mode": mode,
        "workers": workers,
        "initial_stock": stock,
        "successes": len(successes),
        "failures": len(failures),
        "errors": len(errors),
        "final_stock": product.stock,
        "oversold_by": oversold,
        "verdict": "BUG REPRODUCED" if oversold else "OK — stock limit respected",
        "perf": perf.stats(op),
    }


def print_result(r: dict, backend: str) -> None:
    print()
    print(BANNER)
    print(f" Req 7 — Distributed Lock Demo — mode: {r['mode'].upper()}")
    print(f" Lock backend: {backend}")
    print(BANNER)
    print(f" Concurrent workers (servers): {r['workers']}")
    print(f" Initial stock               : {r['initial_stock']}")
    print(f" Successful purchases        : {r['successes']}")
    print(f" Rejected (out_of_stock)     : {r['failures']}")
    print(f" Runtime errors              : {r['errors']}")
    print(f" Final stock in DB           : {r['final_stock']}")
    pf = r["perf"]
    if pf.get("count"):
        print(f" Critical-section latency    : p50 {pf['p50_ms']} ms / "
              f"p95 {pf['p95_ms']} ms  (AOP @measure)")
    print("-" * 70)
    if r["oversold_by"]:
        print(f" !! OVERSOLD by {r['oversold_by']} units  -> race reproduced across workers")
    else:
        print(" OK — stock limit respected, no overselling.")
    print(BANNER)


def print_comparison(before: dict, after: dict, backend: str) -> None:
    print()
    print(BANNER)
    print(" SIDE-BY-SIDE — Before vs After the Distributed Lock")
    print(BANNER)
    print(f"  {'Metric':<28}{'BEFORE (no lock)':>18}{'AFTER (d-lock)':>18}")
    print(f"  {'-'*28}{'-'*18:>18}{'-'*18:>18}")
    print(f"  {'Workers':<28}{before['workers']:>18}{after['workers']:>18}")
    print(f"  {'Initial stock':<28}{before['initial_stock']:>18}{after['initial_stock']:>18}")
    print(f"  {'Successful purchases':<28}{before['successes']:>18}{after['successes']:>18}")
    print(f"  {'Final stock in DB':<28}{before['final_stock']:>18}{after['final_stock']:>18}")
    print(f"  {'Oversold by':<28}{before['oversold_by']:>18}{after['oversold_by']:>18}")
    print(f"  {'Verdict':<28}{before['verdict']:>18}{after['verdict']:>18}")
    print()
    print(f"  RESULT: overselling went from {before['oversold_by']} units to "
          f"{after['oversold_by']} units.")
    if backend != BACKEND_REDIS:
        print("  NOTE  : running on the in-process FALLBACK (no Redis). Start Redis")
        print("          to demonstrate the lock holding across real processes.")


def write_report(results: list[dict], workers: int, stock: int, backend: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORT_DIR / f"req7_distributed_lock_{ts}.md"
    by_mode = {r["mode"]: r for r in results}

    L: list[str] = []
    L.append("# Requirement 7 — Concurrency Control (Distributed Lock)")
    L.append("")
    L.append(f"**Generated:** {datetime.now().isoformat(timespec='seconds')}  ")
    L.append(f"**Workers (simulated servers):** {workers}  ")
    L.append(f"**Initial stock:** {stock}  ")
    L.append(f"**Lock backend:** `{backend}`")
    L.append("")
    L.append("## Scenario")
    L.append(
        "N worker threads each stand in for a request arriving at one of several "
        "application servers (Requirement 5). They are released together by a "
        "`threading.Barrier` and each tries to buy 1 unit. The critical section "
        "reads the stock into a local variable, waits, then writes it back — the "
        "read-modify-write that an in-process lock cannot protect once requests "
        "are spread across servers."
    )
    L.append("")
    L.append("## Results")
    L.append("")
    L.append("| Mode | Workers | Initial | Successes | Rejected | Final stock | Oversold | Verdict |")
    L.append("|------|--------:|--------:|----------:|---------:|------------:|---------:|---------|")
    for r in results:
        L.append(
            f"| **{r['mode'].upper()}** | {r['workers']} | {r['initial_stock']} | "
            f"{r['successes']} | {r['failures']} | {r['final_stock']} | "
            f"{r['oversold_by']} | {r['verdict']} |"
        )
    L.append("")

    if "unsafe" in by_mode and "safe" in by_mode:
        b, a = by_mode["unsafe"], by_mode["safe"]
        L.append("## Before vs After")
        L.append("")
        L.append("| Metric | BEFORE (no lock) | AFTER (distributed lock) |")
        L.append("|--------|-----------------:|-------------------------:|")
        L.append(f"| Successful purchases | {b['successes']} | {a['successes']} |")
        L.append(f"| Final stock in DB | {b['final_stock']} | {a['final_stock']} |")
        L.append(f"| **Oversold by** | **{b['oversold_by']}** | **{a['oversold_by']}** |")
        L.append(f"| Verdict | {b['verdict']} | {a['verdict']} |")
        L.append("")
        L.append(
            f"**Verdict:** the distributed lock eliminated overselling "
            f"({b['oversold_by']} → {a['oversold_by']} units). Stock can never go "
            f"below zero because only one worker — on any server — is inside the "
            f"critical section at a time."
        )
        L.append("")
        for tag, r in (("BEFORE", b), ("AFTER", a)):
            pf = r["perf"]
            if pf.get("count"):
                L.append(f"- **{tag} critical-section latency (AOP @measure):** "
                         f"p50 {pf['p50_ms']} ms / p95 {pf['p95_ms']} ms / "
                         f"max {pf['max_ms']} ms over {pf['count']} calls.")
        L.append("")

    if backend != BACKEND_REDIS:
        L.append("> **Note:** this run used the in-process fallback (no Redis "
                 "reachable). It still serializes the threads in this process, but "
                 "it is **not** a cross-process lock. Start Redis "
                 "(`docker run --rm -p 6379:6379 redis`) and re-run to exercise "
                 "the real distributed lock.")
        L.append("")

    L.append("## Where it lives in the codebase")
    L.append("- Distributed lock primitive: `core/distributed_lock.py::DistributedLock`")
    L.append("- Safe service: `orders/services.py::decrement_stock_dlock_safe`")
    L.append("- Unsafe baseline: `orders/services.py::decrement_stock_dlock_unsafe`")
    L.append("- AOP timing aspect: `core/aop.py::measure` (op `stock.dlock_*`)")
    L.append("")
    path.write_text("\n".join(L), encoding="utf-8")
    return path


class Command(BaseCommand):
    help = "Req 7 demo: cross-process stock race fixed with a Redis distributed lock."

    def add_arguments(self, parser):
        parser.add_argument("--mode", choices=["unsafe", "safe", "both"], default="both")
        parser.add_argument("--workers", type=int, default=50)
        parser.add_argument("--stock", type=int, default=10)
        parser.add_argument("--no-report", action="store_true")

    def handle(self, *args, **opts):
        reset_redis_state()
        backend = active_backend()
        modes = ["unsafe", "safe"] if opts["mode"] == "both" else [opts["mode"]]
        results = []
        for m in modes:
            r = run_scenario(m, opts["workers"], opts["stock"])
            print_result(r, backend)
            results.append(r)

        if len(results) == 2:
            print_comparison(results[0], results[1], backend)

        if not opts["no_report"]:
            path = write_report(results, opts["workers"], opts["stock"], backend)
            print(f"\nReport saved to: {path}")
