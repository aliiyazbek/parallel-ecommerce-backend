"""Requirement #6 demo — Distributed Caching (Redis, cache-aside).

Hammers a single popular product with many concurrent reads and compares:

    BEFORE : every read goes to the database (no cache).
    AFTER  : reads go through a Redis distributed cache (cache-aside); only the
             first read (miss) touches the DB, the rest are served from cache.

Prints a BEFORE vs AFTER table + writes a Markdown report, matching the style of
the other demos. All latency/hit numbers come from the AOP `@measure` aspect and
the cache hit/miss counters — no timing code lives in the services.

    python manage.py demo_distributed_cache
    python manage.py demo_distributed_cache --reads 2000 --threads 50

Start real Redis first for a TRUE distributed cache (otherwise it falls back to
an in-process cache and the report says so):

    docker run --rm -p 6379:6379 redis          # or run Memurai on Windows
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

from core.aop import perf
from core import cache
from products.models import Category, Product
from products.services import (
    DB_QUERY_SECONDS,
    get_product_cached,
    get_product_uncached,
)

DEMO_SLUG = "cache-demo-product"
REPORT_DIR = Path(settings.BASE_DIR) / "reports"
BANNER = "=" * 72
SUB = "-" * 72


def reset_product() -> Product:
    cat, _ = Category.objects.get_or_create(name="Demo", defaults={"slug": "demo"})
    Product.objects.filter(slug=DEMO_SLUG).delete()
    return Product.objects.create(
        category=cat,
        name="Cache Demo Product",
        slug=DEMO_SLUG,
        description="Used only by manage.py demo_distributed_cache.",
        price=100,
        stock=1000,
        is_active=True,
    )


def run_scenario(label: str, read_fn, product_id: int, reads: int,
                 threads: int) -> dict:
    """Fire `reads` reads spread over `threads` worker threads, all racing."""
    # Reset the AOP perf samples and cache counters so each scenario is clean.
    perf.reset()
    cache.reset_cache_stats()
    cache.clear_all()

    counter = {"n": 0}
    counter_lock = threading.Lock()
    barrier = threading.Barrier(threads)

    def worker():
        barrier.wait()
        while True:
            with counter_lock:
                if counter["n"] >= reads:
                    return
                counter["n"] += 1
            read_fn(product_id)
        # connection cleanup happens in finally of the thread body below

    def worker_safe():
        try:
            worker()
        finally:
            connection.close()

    ts = [threading.Thread(target=worker_safe) for _ in range(threads)]
    wall0 = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    wall_ms = (time.perf_counter() - wall0) * 1000

    # Read everything back from the AOP aspect + cache counters.
    db = perf.stats("db.product_query")
    read_op = "product.read_cached" if "AFTER" in label else "product.read_uncached"
    read_stats = perf.stats(read_op)
    cstats = cache.cache_stats()

    return {
        "label": label,
        "reads": reads,
        "threads": threads,
        "wall_ms": round(wall_ms),
        "db_queries": db.get("count", 0),
        "read_p50": read_stats.get("p50_ms"),
        "read_p95": read_stats.get("p95_ms"),
        "read_mean": read_stats.get("mean_ms"),
        "hits": cstats["hits"],
        "misses": cstats["misses"],
        "hit_ratio": cstats["hit_ratio"],
    }


def print_row(r: dict) -> None:
    print(f"  {r['label']:<26} wall={r['wall_ms']:>6}ms  "
          f"db_queries={r['db_queries']:>5}  "
          f"p50={str(r['read_p50']):>6}ms  p95={str(r['read_p95']):>6}ms  "
          f"hit_ratio={r['hit_ratio']*100:>5.1f}%")


def write_report(before: dict, after: dict, reads: int, threads: int,
                 backend: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORT_DIR / f"req6_distributed_cache_{ts}.md"

    db_saved = before["db_queries"] - after["db_queries"]
    db_saved_pct = (db_saved / before["db_queries"] * 100) if before["db_queries"] else 0
    speedup = (before["wall_ms"] / after["wall_ms"]) if after["wall_ms"] else 0

    L: list[str] = []
    L.append("# Requirement 6 — Distributed Caching (Redis, cache-aside)")
    L.append("")
    L.append(f"**Generated:** {datetime.now().isoformat(timespec='seconds')}  ")
    L.append(f"**Cache backend:** `{backend}`  ")
    L.append(f"**Workload:** {reads} reads of ONE popular product over "
             f"{threads} concurrent threads  ")
    L.append(f"**Simulated DB query cost:** {DB_QUERY_SECONDS*1000:.0f} ms each")
    if "fallback" in backend:
        L.append("")
        L.append("> ⚠️ Redis was not reachable, so this run used the in-process "
                 "fallback cache. The numbers still show the cache-aside effect, "
                 "but a real deployment must run Redis for the cache to be shared "
                 "across nodes. Start it with `docker run --rm -p 6379:6379 redis`.")
    L.append("")
    L.append("## Scenario")
    L.append(
        "Many users read the same hot product at once. **BEFORE** every read "
        "runs the database query. **AFTER** reads use cache-aside: the first "
        "read misses and loads from the DB, every later read is served from the "
        "distributed cache and skips the database entirely."
    )
    L.append("")
    L.append("## Before vs After")
    L.append("")
    L.append("| Metric | BEFORE (no cache) | AFTER (Redis cache) |")
    L.append("|--------|------------------:|--------------------:|")
    L.append(f"| Reads | {before['reads']} | {after['reads']} |")
    L.append(f"| **Database queries** | **{before['db_queries']}** | **{after['db_queries']}** |")
    L.append(f"| Cache hits | {before['hits']} | {after['hits']} |")
    L.append(f"| Cache misses | {before['misses']} | {after['misses']} |")
    L.append(f"| Hit ratio | {before['hit_ratio']*100:.1f}% | {after['hit_ratio']*100:.1f}% |")
    L.append(f"| Read latency p50 (ms) | {before['read_p50']} | {after['read_p50']} |")
    L.append(f"| Read latency p95 (ms) | {before['read_p95']} | {after['read_p95']} |")
    L.append(f"| Wall time (ms) | {before['wall_ms']} | {after['wall_ms']} |")
    L.append("")
    L.append(
        f"**Verdict:** the cache eliminated **{db_saved}** of "
        f"**{before['db_queries']}** database queries "
        f"(**{db_saved_pct:.1f}%** fewer), reaching a "
        f"**{after['hit_ratio']*100:.1f}%** hit ratio, and cut total wall time "
        f"by **{speedup:.1f}x**. After the first miss, popular-product reads no "
        f"longer touch the database at all — exactly the goal of Requirement 6."
    )
    L.append("")
    L.append("## How AOP produced these numbers")
    L.append(
        "Every cache operation (`cache.get/set/delete`) and the DB query "
        "(`db.product_query`) are wrapped by the `@measure` aspect in "
        "`core/aop.py`. The aspect records one latency sample per call into a "
        "thread-safe collector; this command reads `perf.stats(...)` and the "
        "cache hit/miss counters afterwards. No timing code lives inside the "
        "cache or the product services."
    )
    L.append("")
    L.append("## Why a *distributed* cache (not a per-process dict)")
    L.append(
        "Under Load Distribution (Req 5) there are several application nodes. A "
        "local dict in node A is invisible to node B, so each node would keep a "
        "separate, possibly stale copy. A shared Redis cache gives every node "
        "ONE view and ONE place to invalidate on a write."
    )
    L.append("")
    L.append("## Cache invalidation (consistency)")
    L.append(
        "`products/services.py::invalidate_product` deletes the cached entry on "
        "any product change. It is wired into `products/views.py::ProductViewSet."
        "perform_update / perform_destroy`, so an admin edit drops the stale "
        "entry and the next read reloads fresh data on every node."
    )
    L.append("")
    L.append("## Where it lives in the codebase")
    L.append("- Cache layer: `core/cache.py` (Redis + in-process fallback)")
    L.append("- Cached vs uncached reads: `products/services.py`")
    L.append("- Invalidation on write: `products/views.py::ProductViewSet`")
    L.append("- This demo: `orders/management/commands/demo_distributed_cache.py`")
    L.append("")

    path.write_text("\n".join(L), encoding="utf-8")
    return path


class Command(BaseCommand):
    help = "Requirement 6 demo: distributed caching (Redis cache-aside), before/after."

    def add_arguments(self, parser):
        parser.add_argument("--reads", type=int, default=1000)
        parser.add_argument("--threads", type=int, default=20)
        parser.add_argument("--no-report", action="store_true")

    def handle(self, *args, **opts):
        cache.reset_redis_state()
        backend = cache.active_backend()
        reads = opts["reads"]
        threads = opts["threads"]

        product = reset_product()

        print()
        print(BANNER)
        print(" Req 6 - Distributed Caching - BEFORE vs AFTER")
        print(BANNER)
        print(f" Cache backend : {backend}")
        print(f" Workload      : {reads} reads of one product over {threads} threads")
        print(f" DB query cost : {DB_QUERY_SECONDS*1000:.0f} ms each (simulated)")
        if "fallback" in backend:
            print(" NOTE: Redis not reachable - using in-process fallback cache.")
            print("       Start Redis for a real distributed cache:")
            print("       docker run --rm -p 6379:6379 redis")
        print(SUB)

        before = run_scenario("BEFORE - no cache", get_product_uncached,
                              product.pk, reads, threads)
        print_row(before)

        after = run_scenario("AFTER - Redis cache", get_product_cached,
                             product.pk, reads, threads)
        print_row(after)

        # ── side-by-side ──
        db_saved = before["db_queries"] - after["db_queries"]
        db_saved_pct = (db_saved / before["db_queries"] * 100) if before["db_queries"] else 0
        speedup = (before["wall_ms"] / after["wall_ms"]) if after["wall_ms"] else 0

        print()
        print(BANNER)
        print(" SIDE-BY-SIDE - Before vs After")
        print(BANNER)
        print(f"  {'Metric':<22}{'BEFORE':>14}{'AFTER':>14}")
        print(f"  {'-'*22}{'-'*14:>14}{'-'*14:>14}")
        print(f"  {'Reads':<22}{before['reads']:>14}{after['reads']:>14}")
        print(f"  {'Database queries':<22}{before['db_queries']:>14}{after['db_queries']:>14}")
        print(f"  {'Hit ratio':<22}{before['hit_ratio']*100:>13.1f}%{after['hit_ratio']*100:>13.1f}%")
        print(f"  {'Read p50 (ms)':<22}{str(before['read_p50']):>14}{str(after['read_p50']):>14}")
        print(f"  {'Read p95 (ms)':<22}{str(before['read_p95']):>14}{str(after['read_p95']):>14}")
        print(f"  {'Wall time (ms)':<22}{before['wall_ms']:>14}{after['wall_ms']:>14}")
        print()
        print(f"  RESULT: cache removed {db_saved}/{before['db_queries']} DB queries "
              f"({db_saved_pct:.1f}% fewer), {speedup:.1f}x faster wall time, "
              f"hit ratio {after['hit_ratio']*100:.1f}%.")

        if not opts["no_report"]:
            path = write_report(before, after, reads, threads, backend)
            print(f"\nReport saved to: {path}")
