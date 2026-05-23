from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

from orders.services import decrement_stock_safe, decrement_stock_unsafe
from products.models import Category, Product

DEMO_SLUG = "race-demo-product"
REPORT_DIR = Path(settings.BASE_DIR) / "reports"


def reset_product(stock: int) -> Product:
    cat, _ = Category.objects.get_or_create(name="Demo", defaults={"slug": "demo"})
    Product.objects.filter(slug=DEMO_SLUG).delete()
    return Product.objects.create(
        category=cat,
        name="Race Demo Product",
        slug=DEMO_SLUG,
        description="Used only by manage.py demo_stock_race.",
        price=100,
        stock=stock,
        is_active=True,
    )


def run_scenario(mode: str, threads: int, stock: int) -> dict:
    product = reset_product(stock)
    fn = decrement_stock_safe if mode == "safe" else decrement_stock_unsafe

    successes: list[int] = []
    failures: list[tuple[int, str]] = []
    errors: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(threads)

    def worker(i: int):
        try:
            barrier.wait()
            ok, reason = fn(product.pk, 1)
            with lock:
                if ok:
                    successes.append(i)
                else:
                    failures.append((i, reason))
        except Exception as exc:
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            connection.close()

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    product.refresh_from_db()
    oversold = max(0, len(successes) - stock)

    return {
        "mode": mode,
        "threads": threads,
        "initial_stock": stock,
        "successes": len(successes),
        "failures": len(failures),
        "errors": len(errors),
        "final_stock": product.stock,
        "oversold_by": oversold,
        "verdict": "BUG REPRODUCED" if oversold else "OK — stock limit respected",
        "error_samples": errors[:3],
    }


def print_result(r: dict) -> None:
    print()
    print("=" * 66)
    print(f" Task 1 — Stock Race Demo — mode: {r['mode'].upper()}")
    print("=" * 66)
    print(f" Threads (concurrent buyers) : {r['threads']}")
    print(f" Initial stock               : {r['initial_stock']}")
    print(f" Successful purchases        : {r['successes']}")
    print(f" Rejected (out_of_stock)     : {r['failures']}")
    print(f" Runtime errors              : {r['errors']}")
    print(f" Final stock in DB           : {r['final_stock']}")
    print("-" * 66)
    if r["oversold_by"]:
        print(f" !! OVERSOLD by {r['oversold_by']} units  -> Race Condition reproduced")
    else:
        print(" OK — stock limit respected, no overselling.")
    for e in r["error_samples"]:
        print(f"   error: {e}")
    print("=" * 66)


def write_report(results: list[dict], threads: int, stock: int) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORT_DIR / f"task1_stock_race_{ts}.md"

    lines: list[str] = []
    lines.append("# Task 1 — Concurrent Access & Data Integrity (Product Stock)")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now().isoformat(timespec='seconds')}  ")
    lines.append(f"**Threads:** {threads}  ")
    lines.append(f"**Initial stock:** {stock}")
    lines.append("")
    lines.append("## Scenario")
    lines.append(
        "We reset a single product to a known stock, then launch N threads "
        "simultaneously that each try to buy 1 unit. A `threading.Barrier` "
        "releases all threads at the same instant to maximise contention."
    )
    lines.append("")
    lines.append("## Results")
    lines.append("")
    lines.append("| Mode | Threads | Initial stock | Successes | Rejected | Final stock | Oversold | Verdict |")
    lines.append("|------|---------|---------------|-----------|----------|-------------|----------|---------|")
    for r in results:
        lines.append(
            f"| **{r['mode'].upper()}** | {r['threads']} | {r['initial_stock']} | "
            f"{r['successes']} | {r['failures']} | {r['final_stock']} | "
            f"{r['oversold_by']} | {r['verdict']} |"
        )
    lines.append("")
    lines.append("## Interpretation")
    lines.append("")
    lines.append(
        "- **UNSAFE**: read-modify-write with no locking. Concurrent callers "
        "read the same stock value, both pass the availability check, and "
        "both decrement -> **overselling** (final stock can be negative)."
    )
    lines.append(
        "- **SAFE**: wraps the same logic in `transaction.atomic()` and uses "
        "`select_for_update()` to take a **pessimistic row-level lock**. "
        "Concurrent callers queue on the lock and re-read the fresh stock, "
        "so the limit is never violated."
    )
    lines.append("")
    lines.append("## Where the fix lives in the codebase")
    lines.append("- Safe service: `orders/services.py::decrement_stock_safe`")
    lines.append("- Unsafe service (demo only): `orders/services.py::decrement_stock_unsafe`")
    lines.append("- Applied in production path: `orders/views.py::CheckoutView.post` "
                 "(locks all cart product rows before reading/writing stock).")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class Command(BaseCommand):
    help = "Task 1 demo: reproduce and fix the product-stock race condition."

    def add_arguments(self, parser):
        parser.add_argument("--mode", choices=["unsafe", "safe", "both"], default="both")
        parser.add_argument("--threads", type=int, default=50)
        parser.add_argument("--stock", type=int, default=10)
        parser.add_argument("--no-report", action="store_true",
                            help="Skip writing the Markdown report file.")

    def handle(self, *args, **opts):
        modes = ["unsafe", "safe"] if opts["mode"] == "both" else [opts["mode"]]
        results = []
        for m in modes:
            r = run_scenario(m, opts["threads"], opts["stock"])
            print_result(r)
            results.append(r)

        if not opts["no_report"]:
            path = write_report(results, opts["threads"], opts["stock"])
            print(f"\nReport saved to: {path}")
