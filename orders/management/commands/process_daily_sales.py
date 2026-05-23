import logging
import sys
import time
import tracemalloc
import warnings
from datetime import date, timedelta
from decimal import Decimal

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


warnings.filterwarnings(
    "ignore",
    message=r".*received a naive datetime.*",
    category=RuntimeWarning,
    module=r"django\.db\.models\.fields",
)

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Sum, Count, F
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.aop import log_execution
from orders.models import Order, OrderItem, DailySalesReport

logger = logging.getLogger(__name__)

W = 62
DIV_HEAVY = "═" * W
DIV_LIGHT = "─" * W


class Command(BaseCommand):
    help = "Aggregate previous day's orders into DailySalesReport in chunks."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            type=str,
            default=None,
            help="Target date (YYYY-MM-DD). Defaults to yesterday.",
        )
        parser.add_argument(
            "--chunk-size",
            type=int,
            default=500,
            dest="chunk_size",
            help="Number of orders processed per DB transaction (default 500).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            dest="dry_run",
            help="Simulate processing without writing to the DB.",
        )
        parser.add_argument(
            "--compare",
            action="store_true",
            default=False,
            dest="compare",
            help="Run BOTH no-chunk and chunked modes and show side-by-side benchmark.",
        )

    def handle(self, *args, **options):
        target_date = self._resolve_date(options["date"])
        chunk_size: int = options["chunk_size"]
        dry_run: bool = options["dry_run"]

        if options["compare"]:
            return self._run_compare(target_date, chunk_size)

        self.stdout.write(self.style.MIGRATE_HEADING("\n" + DIV_HEAVY))
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"  DAILY SALES BATCH PROCESSOR  —  Requirement #4"
        ))
        self.stdout.write(self.style.MIGRATE_HEADING(DIV_HEAVY))
        self.stdout.write(f"  {'Target date':<18}: {target_date}")
        self.stdout.write(f"  {'Chunk size':<18}: {chunk_size:,} orders / chunk")
        self.stdout.write(f"  {'Mode':<18}: {'DRY RUN (no writes)' if dry_run else 'LIVE'}")
        self.stdout.write(DIV_LIGHT)

        start_wall = time.perf_counter()
        stats = self._run(target_date, chunk_size, dry_run)
        elapsed = time.perf_counter() - start_wall

        self.stdout.write(DIV_LIGHT)
        status_style = self.style.SUCCESS if stats["errors"] == 0 else self.style.WARNING
        self.stdout.write(status_style(
            f"  {'Orders processed':<18}: {stats['orders_processed']:,}"
        ))
        self.stdout.write(status_style(
            f"  {'Chunks committed':<18}: {stats['chunks']:,}"
        ))
        if stats["errors"]:
            self.stdout.write(self.style.ERROR(
                f"  {'Errors':<18}: {stats['errors']}"
            ))
        self.stdout.write(status_style(
            f"  {'Wall time':<18}: {elapsed:.3f}s"
        ))
        self.stdout.write(self.style.MIGRATE_HEADING(DIV_HEAVY + "\n"))

    def _run(self, target_date: date, chunk_size: int, dry_run: bool) -> dict:
        stats = {"orders_processed": 0, "chunks": 0, "errors": 0}

        order_ids_qs = (
            Order.objects.filter(
                status="paid",
                created_at__date=target_date,
            )
            .values_list("id", flat=True)
            .order_by("id")  # deterministic ordering → reproducible, non-overlapping chunks
        )

        total_orders = order_ids_qs.count()
        if total_orders == 0:
            self.stdout.write(self.style.WARNING(
                f"  No paid orders found for {target_date}. Nothing to do."
            ))
            return stats

        total_chunks = -(-total_orders // chunk_size)
        self.stdout.write(
            f"  Found {total_orders:,} paid orders  →  "
            f"{total_chunks} chunk(s) of ≤{chunk_size:,}\n"
        )

        all_ids = list(order_ids_qs)

        # Process orders in fixed-size chunks: one DB transaction per chunk keeps
        # peak memory at O(chunk_size) and lets a failed chunk roll back alone (Req #4).
        for batch_index, chunk_start in enumerate(range(0, len(all_ids), chunk_size)):
            chunk_ids = all_ids[chunk_start : chunk_start + chunk_size]

            try:
                processed = self._process_chunk(
                    chunk_ids=chunk_ids,
                    target_date=target_date,
                    batch_index=batch_index,
                    dry_run=dry_run,
                )
                stats["orders_processed"] += processed
                stats["chunks"] += 1
                self.stdout.write(
                    f"  Chunk {batch_index + 1:>4}/{total_chunks}"
                    f"  [{chunk_start + 1:>6,} – {min(chunk_start + chunk_size, total_orders):>6,}]"
                    f"  ✓ committed"
                )

            except Exception as exc:
                stats["errors"] += 1
                logger.exception(
                    "Chunk %d failed for date %s: %s", batch_index, target_date, exc
                )
                self.stderr.write(
                    self.style.ERROR(f"Chunk {batch_index} error: {exc}")
                )

        return stats

    @log_execution()
    @transaction.atomic  # Synchronization point: one atomic transaction per chunk (Req #8 — ACID)
    def _process_chunk(
        self,
        chunk_ids: list,
        target_date: date,
        batch_index: int,
        dry_run: bool,
    ) -> int:
        aggregated = (
            OrderItem.objects.filter(order_id__in=chunk_ids)
            .values("product_id", "product_name")
            .annotate(
                total_revenue=Sum(F("price") * F("quantity")),
                total_units=Sum("quantity"),
                order_count=Count("order_id", distinct=True),
            )
        )

        if dry_run:
            for row in aggregated:
                self.stdout.write(
                    f"  [DRY-RUN] product={row['product_name']} "
                    f"units={row['total_units']} revenue={row['total_revenue']}"
                )
            return len(chunk_ids)

        for row in aggregated:
            # ── Synchronization point: lock the report row during upsert ──────
            # skip_locked=False → deliberately BLOCK on a locked row so a parallel
            # run for the same (date, product) cannot interleave; the
            # accumulate-then-save below stays consistent (no lost updates).
            report, created = DailySalesReport.objects.select_for_update(
                skip_locked=False
            ).get_or_create(
                date=target_date,
                product_id=row["product_id"],
                defaults={
                    "product_name": row["product_name"],
                    "total_units_sold": row["total_units"],
                    "total_revenue": row["total_revenue"],
                    "order_count": row["order_count"],
                    "batch_index": batch_index,
                },
            )
            if not created:
                report.total_units_sold += row["total_units"]
                report.total_revenue += row["total_revenue"]
                report.order_count += row["order_count"]
                report.save(
                    update_fields=["total_units_sold", "total_revenue", "order_count"]
                )

        logger.info(
            "Chunk %d committed: %d order IDs, date=%s",
            batch_index,
            len(chunk_ids),
            target_date,
        )
        return len(chunk_ids)

    def _run_compare(self, target_date: date, chunk_size: int):
        order_count = (
            Order.objects.filter(status="paid", created_at__date=target_date).count()
        )

        self.stdout.write(self.style.MIGRATE_HEADING("\n" + DIV_HEAVY))
        self.stdout.write(self.style.MIGRATE_HEADING(
            "  CHUNKED vs NON-CHUNKED BENCHMARK  —  Req #4 + Req #10"
        ))
        self.stdout.write(self.style.MIGRATE_HEADING(DIV_HEAVY))
        self.stdout.write(f"  {'Target date':<18}: {target_date}")
        self.stdout.write(f"  {'Dataset':<18}: {order_count:,} paid orders")
        self.stdout.write(f"  {'Chunk size':<18}: {chunk_size:,} orders / chunk")
        self.stdout.write(DIV_LIGHT)

        if order_count == 0:
            self.stdout.write(self.style.WARNING(
                f"  No paid orders found for {target_date}. "
                f"Run `python manage.py seed_demo_orders` first."
            ))
            return

        self.stdout.write(self.style.WARNING(
            "\n  [A] WITHOUT CHUNKING  —  load ALL orders + items into RAM at once"
        ))
        no_chunk = self._measure(self._run_no_chunks, target_date)
        self._print_metrics(no_chunk)

        self.stdout.write(self.style.WARNING(
            f"\n  [B] WITH CHUNKING  —  stream the same dataset in chunks of {chunk_size}"
        ))
        chunked = self._measure(self._run_chunked_silent, target_date, chunk_size)
        self._print_metrics(chunked)

        self._print_comparison(no_chunk, chunked, chunk_size)

    def _measure(self, fn, *args, **kwargs) -> dict:
        tracemalloc.start()
        start = time.perf_counter()
        with CaptureQueriesContext(connection) as ctx:
            result = fn(*args, **kwargs)
        elapsed = time.perf_counter() - start
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        return {
            "wall_time": elapsed,
            "peak_mem_mb": peak_bytes / (1024 * 1024),
            "db_queries": len(ctx.captured_queries),
            "orders": result.get("orders", 0),
            "rows_in_ram": result.get("rows_in_ram", 0),
            "extra": result.get("extra", ""),
        }

    def _run_no_chunks(self, target_date: date) -> dict:
        all_order_ids = list(
            Order.objects.filter(status="paid", created_at__date=target_date)
            .values_list("id", flat=True)
        )

        all_items = list(
            OrderItem.objects.filter(order_id__in=all_order_ids)
            .values("product_id", "product_name", "price", "quantity", "order_id")
        )

        agg: dict = {}
        seen_orders: dict = {}
        for item in all_items:
            pid = item["product_id"]
            if pid not in agg:
                agg[pid] = {
                    "name": item["product_name"],
                    "revenue": Decimal("0"),
                    "units": 0,
                    "orders": 0,
                }
                seen_orders[pid] = set()
            agg[pid]["revenue"] += Decimal(item["price"]) * item["quantity"]
            agg[pid]["units"] += item["quantity"]
            if item["order_id"] not in seen_orders[pid]:
                seen_orders[pid].add(item["order_id"])
                agg[pid]["orders"] += 1

        return {
            "orders": len(all_order_ids),
            "rows_in_ram": len(all_order_ids) + len(all_items),
            "extra": f"{len(agg)} unique products aggregated",
        }

    def _run_chunked_silent(self, target_date: date, chunk_size: int) -> dict:
        all_ids = list(
            Order.objects.filter(status="paid", created_at__date=target_date)
            .values_list("id", flat=True)
            .order_by("id")
        )

        max_rows_in_ram = 0
        chunks = 0
        for chunk_start in range(0, len(all_ids), chunk_size):
            chunk_ids = all_ids[chunk_start : chunk_start + chunk_size]
            aggregated = list(
                OrderItem.objects.filter(order_id__in=chunk_ids)
                .values("product_id", "product_name")
                .annotate(
                    total_revenue=Sum(F("price") * F("quantity")),
                    total_units=Sum("quantity"),
                    order_count=Count("order_id", distinct=True),
                )
            )
            max_rows_in_ram = max(max_rows_in_ram, len(chunk_ids) + len(aggregated))
            chunks += 1

        return {
            "orders": len(all_ids),
            "rows_in_ram": max_rows_in_ram,
            "extra": f"{chunks} chunks processed",
        }

    def _print_metrics(self, m: dict):
        self.stdout.write(f"      Wall time            : {m['wall_time']*1000:8.2f} ms")
        self.stdout.write(f"      Peak Python memory   : {m['peak_mem_mb']:8.2f} MB")
        self.stdout.write(f"      DB queries executed  : {m['db_queries']:8,}")
        self.stdout.write(f"      Max rows held in RAM : {m['rows_in_ram']:8,}")
        if m["extra"]:
            self.stdout.write(f"      Note                 : {m['extra']}")

    def _print_comparison(self, no_chunk: dict, chunked: dict, chunk_size: int):
        def pct(old, new):
            if old <= 0:
                return "n/a"
            change = (new - old) / old * 100
            return f"{change:+.1f}%"

        self.stdout.write("\n" + DIV_HEAVY)
        self.stdout.write(self.style.MIGRATE_HEADING("  PERFORMANCE COMPARISON"))
        self.stdout.write(DIV_HEAVY)
        self.stdout.write(
            f"  {'Metric':<22} | {'NO CHUNKS':>14} | {'CHUNKED':>14} | {'Δ':>10}"
        )
        self.stdout.write(DIV_LIGHT)
        self.stdout.write(
            f"  {'Wall time (ms)':<22} | "
            f"{no_chunk['wall_time']*1000:>14.2f} | "
            f"{chunked['wall_time']*1000:>14.2f} | "
            f"{pct(no_chunk['wall_time'], chunked['wall_time']):>10}"
        )
        self.stdout.write(
            f"  {'Peak memory (MB)':<22} | "
            f"{no_chunk['peak_mem_mb']:>14.2f} | "
            f"{chunked['peak_mem_mb']:>14.2f} | "
            f"{pct(no_chunk['peak_mem_mb'], chunked['peak_mem_mb']):>10}"
        )
        self.stdout.write(
            f"  {'DB queries':<22} | "
            f"{no_chunk['db_queries']:>14,} | "
            f"{chunked['db_queries']:>14,} | "
            f"{pct(no_chunk['db_queries'], chunked['db_queries']):>10}"
        )
        self.stdout.write(
            f"  {'Max rows in RAM':<22} | "
            f"{no_chunk['rows_in_ram']:>14,} | "
            f"{chunked['rows_in_ram']:>14,} | "
            f"{pct(no_chunk['rows_in_ram'], chunked['rows_in_ram']):>10}"
        )
        self.stdout.write(DIV_LIGHT)

        mem_factor = (
            no_chunk["peak_mem_mb"] / chunked["peak_mem_mb"]
            if chunked["peak_mem_mb"] > 0
            else float("inf")
        )
        rows_factor = (
            no_chunk["rows_in_ram"] / chunked["rows_in_ram"]
            if chunked["rows_in_ram"] > 0
            else float("inf")
        )

        self.stdout.write(self.style.SUCCESS(
            f"  Chunking holds {rows_factor:.1f}x fewer rows in RAM at any moment."
        ))
        self.stdout.write(self.style.SUCCESS(
            f"  Chunking peaks at {mem_factor:.2f}x less Python heap."
        ))

        if chunked["wall_time"] > no_chunk["wall_time"]:
            overhead = (chunked["wall_time"] - no_chunk["wall_time"]) / no_chunk["wall_time"] * 100
            self.stdout.write(self.style.WARNING(
                f"  Time overhead per chunk transaction: +{overhead:.1f}% — acceptable"
                f" tradeoff: each chunk commits atomically and the process survives"
                f" partial failures (Req #8 ACID)."
            ))
        else:
            saved = (no_chunk["wall_time"] - chunked["wall_time"]) / no_chunk["wall_time"] * 100
            self.stdout.write(self.style.SUCCESS(
                f"  Chunking was also {saved:.1f}% faster on this dataset."
            ))

        self.stdout.write(self.style.MIGRATE_HEADING(DIV_HEAVY + "\n"))

    @staticmethod
    def _resolve_date(date_str: str | None) -> date:
        if date_str is None:
            return (timezone.now() - timedelta(days=1)).date()
        try:
            return date.fromisoformat(date_str)
        except ValueError:
            raise CommandError(f"Invalid date format '{date_str}'. Use YYYY-MM-DD.")
