"""
Task 10 — Benchmarking & Bottleneck Analysis  (Requirement #10)

What it does
------------
1. Measures the **response time of the most-hit read operation** — the product
   catalogue listing (`GET /api/products/`) — via DRF's `APIClient`.
2. Identifies a concrete **bottleneck**: the classic **N+1 query problem** in
   the product serializer. `ProductSerializer` exposes ``category_name`` through
   ``source="category.name"``, so serialising N products without
   ``select_related("category")`` issues N extra queries (one per product).
3. Reports a numeric **BEFORE vs AFTER** comparison of the fix
   (``select_related("category")``): query count and latency.

The same fix is applied to the live endpoint in
``products/views.py::ProductViewSet.queryset``, so the real API benefits too —
this command proves the gain with hard numbers.

Run:
    python manage.py benchmark                      # 200 products, 30 reps
    python manage.py benchmark --products 1000      # bigger catalogue
"""

import statistics
import time
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.test.utils import CaptureQueriesContext

from rest_framework.test import APIClient

from products.models import Category, Product
from products.serializers import ProductSerializer

REPORT_DIR = Path(settings.BASE_DIR) / "reports"
BANNER = "=" * 72
SUB = "-" * 72
NUM_CATEGORIES = 10


def _percentile(sorted_vals: list[float], q: float):
    if not sorted_vals:
        return None
    k = min(len(sorted_vals) - 1, int(round((len(sorted_vals) - 1) * q)))
    return sorted_vals[k]


def seed(num_products: int) -> int:
    """Ensure at least `num_products` active products exist, each with a
    non-null category (so the N+1 lookup actually fires)."""
    cats = []
    for i in range(NUM_CATEGORIES):
        c, _ = Category.objects.get_or_create(
            name=f"Bench Cat {i}", defaults={"slug": f"bench-cat-{i}"}
        )
        cats.append(c)

    existing = Product.objects.filter(name__startswith="Bench Product").count()
    if existing < num_products:
        bulk = []
        for j in range(existing, num_products):
            bulk.append(
                Product(
                    category=cats[j % NUM_CATEGORIES],
                    name=f"Bench Product {j}",
                    slug=f"bench-product-{j}",   # set explicitly: bulk_create skips save()
                    description="Benchmark fixture row.",
                    price="9.99",
                    stock=1000,
                    is_active=True,
                )
            )
        Product.objects.bulk_create(bulk, batch_size=500)

    return Product.objects.filter(is_active=True).count()


def measure_serialization(use_select_related: bool, limit: int, repeat: int) -> dict:
    """Serialize `limit` products `repeat` times and capture query count +
    latency. `use_select_related` toggles the fix on/off."""

    def build_qs():
        qs = Product.objects.filter(is_active=True)
        if use_select_related:
            qs = qs.select_related("category")
        return qs[:limit]

    # Warm-up (prime connection, compile SQL) — not counted.
    _ = ProductSerializer(list(build_qs()), many=True).data

    times: list[float] = []
    queries = 0
    rows = 0
    for _ in range(repeat):
        with CaptureQueriesContext(connection) as ctx:
            t0 = time.perf_counter()
            data = ProductSerializer(list(build_qs()), many=True).data
            # Touch every row's category_name to force the lazy lookups to run.
            _ = [row["category_name"] for row in data]
            times.append((time.perf_counter() - t0) * 1000)
        queries = len(ctx)
        rows = len(data)

    times.sort()
    return {
        "queries": queries,
        "rows": rows,
        "mean_ms": statistics.fmean(times),
        "p50_ms": _percentile(times, 0.50),
        "min_ms": times[0],
    }


def measure_endpoint(path: str, repeat: int) -> dict:
    """Measure live endpoint response time (after the production fix is in place)."""
    client = APIClient()
    client.get(path)  # warm-up
    times: list[float] = []
    last = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        last = client.get(path)
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    return {
        "status": last.status_code,
        "mean_ms": statistics.fmean(times),
        "p50_ms": _percentile(times, 0.50),
        "p95_ms": _percentile(times, 0.95),
    }


def print_report(cfg, before, after, endpoint) -> None:
    q_drop = before["queries"] - after["queries"]
    q_drop_pct = (q_drop / before["queries"] * 100) if before["queries"] else 0
    speedup = (before["mean_ms"] / after["mean_ms"]) if after["mean_ms"] else 0
    t_drop_pct = (
        (before["mean_ms"] - after["mean_ms"]) / before["mean_ms"] * 100
        if before["mean_ms"] else 0
    )

    print()
    print(BANNER)
    print(" Task 10 — Benchmarking & Bottleneck Analysis  (Requirement #10)")
    print(BANNER)
    print(f" Catalogue size      : {cfg['total_products']} active products")
    print(f" Rows per listing    : {cfg['limit']}")
    print(f" Repetitions         : {cfg['repeat']} (median/mean reported)")
    print(SUB)
    print(" KEY OPERATION — live response time:  GET /api/products/")
    print(f"   HTTP {endpoint['status']}   p50 {endpoint['p50_ms']:.1f} ms   "
          f"p95 {endpoint['p95_ms']:.1f} ms   mean {endpoint['mean_ms']:.1f} ms")
    print(SUB)
    print(" BOTTLENECK: N+1 queries in product listing serialization")
    print(f"   {'Variant':<26}{'Queries':>9}{'Mean (ms)':>12}{'p50 (ms)':>11}")
    print(f"   {'-'*26}{'-'*9:>9}{'-'*12:>12}{'-'*11:>11}")
    print(f"   {'BEFORE (no select_related)':<26}{before['queries']:>9}"
          f"{before['mean_ms']:>12.2f}{before['p50_ms']:>11.2f}")
    print(f"   {'AFTER  (select_related)':<26}{after['queries']:>9}"
          f"{after['mean_ms']:>12.2f}{after['p50_ms']:>11.2f}")
    print(SUB)
    print(" IMPROVEMENT")
    print(f"   Queries : {before['queries']} -> {after['queries']}  "
          f"(-{q_drop}, -{q_drop_pct:.0f}%)")
    print(f"   Latency : {before['mean_ms']:.2f} ms -> {after['mean_ms']:.2f} ms  "
          f"({speedup:.2f}x faster, -{t_drop_pct:.0f}%)")
    print(BANNER)


def write_report(cfg, before, after, endpoint) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORT_DIR / f"task10_benchmark_{ts}.md"

    q_drop = before["queries"] - after["queries"]
    q_drop_pct = (q_drop / before["queries"] * 100) if before["queries"] else 0
    speedup = (before["mean_ms"] / after["mean_ms"]) if after["mean_ms"] else 0
    t_drop_pct = (
        (before["mean_ms"] - after["mean_ms"]) / before["mean_ms"] * 100
        if before["mean_ms"] else 0
    )

    lines: list[str] = []
    lines.append("# Task 10 — Benchmarking & Bottleneck Analysis")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now().isoformat(timespec='seconds')}  ")
    lines.append(f"**Catalogue size:** {cfg['total_products']} active products  ")
    lines.append(f"**Rows per listing:** {cfg['limit']}  ")
    lines.append(f"**Repetitions:** {cfg['repeat']}")
    lines.append("")
    lines.append("## 1. Key operation — measured response time")
    lines.append("")
    lines.append("Live endpoint latency for the catalogue listing (the most-hit read path):")
    lines.append("")
    lines.append("| Operation | Status | p50 (ms) | p95 (ms) | mean (ms) |")
    lines.append("|-----------|:------:|---------:|---------:|----------:|")
    lines.append(
        f"| `GET /api/products/` | {endpoint['status']} | "
        f"{endpoint['p50_ms']:.1f} | {endpoint['p95_ms']:.1f} | {endpoint['mean_ms']:.1f} |"
    )
    lines.append("")
    lines.append("## 2. Bottleneck identified — N+1 queries")
    lines.append("")
    lines.append(
        "`ProductSerializer` exposes `category_name` through "
        "`source=\"category.name\"`. Listing N products **without** "
        "`select_related(\"category\")` issues **one query for the products plus "
        "one extra query per product** to fetch its category — the classic "
        "**N+1 query problem**. It scales linearly with catalogue size and is the "
        "dominant cost of the listing endpoint."
    )
    lines.append("")
    lines.append("## 3. Before vs After the fix")
    lines.append("")
    lines.append(
        f"Both variants serialize the same **{before['rows']} products**, "
        f"averaged over **{cfg['repeat']}** runs."
    )
    lines.append("")
    lines.append("| Variant | DB queries | Mean (ms) | p50 (ms) |")
    lines.append("|---------|-----------:|----------:|---------:|")
    lines.append(
        f"| **BEFORE** — no `select_related` | {before['queries']} | "
        f"{before['mean_ms']:.2f} | {before['p50_ms']:.2f} |"
    )
    lines.append(
        f"| **AFTER** — `select_related(\"category\")` | {after['queries']} | "
        f"{after['mean_ms']:.2f} | {after['p50_ms']:.2f} |"
    )
    lines.append("")
    lines.append("### Improvement")
    lines.append("")
    lines.append("| Metric | Before | After | Gain |")
    lines.append("|--------|-------:|------:|:-----|")
    lines.append(
        f"| DB queries | {before['queries']} | {after['queries']} | "
        f"**−{q_drop} ({q_drop_pct:.0f}% fewer)** |"
    )
    lines.append(
        f"| Mean latency | {before['mean_ms']:.2f} ms | {after['mean_ms']:.2f} ms | "
        f"**{speedup:.2f}× faster (−{t_drop_pct:.0f}%)** |"
    )
    lines.append("")
    lines.append("## 4. The fix")
    lines.append("")
    lines.append("```python")
    lines.append("# products/views.py — ProductViewSet")
    lines.append("queryset = Product.objects.filter(is_active=True).select_related(\"category\")")
    lines.append("```")
    lines.append("")
    lines.append("- Bottleneck + benchmark harness: `orders/management/commands/benchmark.py`")
    lines.append("- Applied fix: `products/views.py::ProductViewSet.queryset`")
    lines.append("- Always-on timing: the AOP `@log_execution` decorator "
                 "(`core/aop.py`) logs per-call latency in production.")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class Command(BaseCommand):
    help = "Task 10: benchmark the product listing, expose the N+1 bottleneck, report before/after."

    def add_arguments(self, parser):
        parser.add_argument("--products", type=int, default=200,
                            help="Number of products to serialize / seed (default: 200).")
        parser.add_argument("--repeat", type=int, default=30,
                            help="Repetitions per variant for averaging (default: 30).")
        parser.add_argument("--no-report", action="store_true",
                            help="Skip writing the Markdown report file.")

    def handle(self, *args, **opts):
        limit = opts["products"]
        repeat = opts["repeat"]

        self.stdout.write(f"Seeding catalogue to >= {limit} products …")
        total = seed(limit)

        cfg = {"total_products": total, "limit": limit, "repeat": repeat}

        self.stdout.write("Measuring live endpoint response time …")
        endpoint = measure_endpoint("/api/products/", repeat)

        self.stdout.write("Benchmarking BEFORE (N+1) …")
        before = measure_serialization(use_select_related=False, limit=limit, repeat=repeat)

        self.stdout.write("Benchmarking AFTER (select_related) …")
        after = measure_serialization(use_select_related=True, limit=limit, repeat=repeat)

        print_report(cfg, before, after, endpoint)

        if not opts["no_report"]:
            path = write_report(cfg, before, after, endpoint)
            self.stdout.write(f"\nReport saved to: {path}")
