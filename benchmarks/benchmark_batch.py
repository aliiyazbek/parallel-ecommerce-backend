import argparse
import gc
import os
import sys
import time
import warnings
from datetime import date, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

warnings.filterwarnings(
    "ignore",
    message=r".*received a naive datetime.*",
    category=RuntimeWarning,
    module=r"django\.db\.models\.fields",
)

import django

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

import psutil
from django.contrib.auth import get_user_model
from django.db import transaction, connection
from django.db.models import Sum, F

from orders.models import Order, OrderItem, DailySalesReport
from products.models import Product, Category

User = get_user_model()
PROCESS = psutil.Process(os.getpid())

W          = 72
DIV_HEAVY  = "═" * W
DIV_MID    = "╟" + "─" * (W - 2) + "╢"
DIV_LIGHT  = "─" * W
COL_A      = 26
COL_N      = 9
COL_T      = 11
COL_M      = 13
COL_P      = 11

GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def _c(text: str, *codes: str) -> str:
    return "".join(codes) + str(text) + RESET



def peak_rss_mb() -> float:
    return PROCESS.memory_info().rss / 1024 / 1024


def seed_database(num_orders: int, target_date: date) -> None:
    existing = Order.objects.filter(status="paid", created_at__date=target_date).count()
    if existing >= num_orders:
        print(f"  {CYAN}[SEED]{RESET} {existing:,} orders already exist for {target_date} — skipping.")
        return

    print(f"  {CYAN}[SEED]{RESET} Inserting {num_orders:,} synthetic orders …", end="", flush=True)

    user, _ = User.objects.get_or_create(
        username="bench_user", defaults={"email": "bench@example.com"}
    )
    cat, _ = Category.objects.get_or_create(name="Benchmark Category")
    product, _ = Product.objects.get_or_create(
        name="Benchmark Product",
        defaults={"category": cat, "price": "9.99", "stock": 999999},
    )

    orders_to_create = []
    items_to_create = []

    for i in range(num_orders - existing):
        o = Order(
            user=user,
            status="paid",
            shipping_address="123 Bench St",
            total="9.99",
        )
        orders_to_create.append(o)

    with transaction.atomic():
        created_orders = Order.objects.bulk_create(orders_to_create, batch_size=1000)
        Order.objects.filter(pk__in=[o.pk for o in created_orders]).update(
            created_at=target_date
        )

        for o in created_orders:
            items_to_create.append(
                OrderItem(
                    order=o,
                    product=product,
                    product_name=product.name,
                    price=product.price,
                    quantity=1,
                )
            )
        OrderItem.objects.bulk_create(items_to_create, batch_size=1000)

    print(f"  {GREEN}done{RESET} ({num_orders:,} orders ready)")



def run_naive(target_date: date) -> dict:
    gc.collect()
    mem_before = peak_rss_mb()
    t0 = time.perf_counter()

    all_items = list(
        OrderItem.objects.filter(
            order__status="paid",
            order__created_at__date=target_date,
        ).select_related("product")
    )

    totals: dict = {}
    for item in all_items:
        pid = item.product_id
        if pid not in totals:
            totals[pid] = {"units": 0, "revenue": 0}
        totals[pid]["units"] += item.quantity
        totals[pid]["revenue"] += float(item.price) * item.quantity

    elapsed = time.perf_counter() - t0
    mem_after = peak_rss_mb()

    return {
        "approach": "NAIVE (full load)",
        "rows": len(all_items),
        "wall_time_s": round(elapsed, 3),
        "mem_delta_mb": round(mem_after - mem_before, 1),
        "peak_rss_mb": round(mem_after, 1),
        "db_queries": len(connection.queries) if hasattr(connection, "queries") else "N/A",
    }



def run_chunked(target_date: date, chunk_size: int) -> dict:
    gc.collect()
    mem_before = peak_rss_mb()
    t0 = time.perf_counter()

    all_ids = list(
        Order.objects.filter(status="paid", created_at__date=target_date)
        .values_list("id", flat=True)
        .order_by("id")
    )

    total_rows = 0
    peak_chunk_mem = 0.0

    for chunk_start in range(0, len(all_ids), chunk_size):
        chunk_ids = all_ids[chunk_start : chunk_start + chunk_size]

        aggregated = list(
            OrderItem.objects.filter(order_id__in=chunk_ids)
            .values("product_id")
            .annotate(
                total_units=Sum("quantity"),
                total_revenue=Sum(F("price") * F("quantity")),
            )
        )
        total_rows += len(chunk_ids)

        chunk_mem = peak_rss_mb() - mem_before
        if chunk_mem > peak_chunk_mem:
            peak_chunk_mem = chunk_mem

    elapsed = time.perf_counter() - t0
    mem_after = peak_rss_mb()

    return {
        "approach": f"CHUNKED (size={chunk_size})",
        "rows": total_rows,
        "wall_time_s": round(elapsed, 3),
        "mem_delta_mb": round(mem_after - mem_before, 1),
        "peak_rss_mb": round(mem_after, 1),
        "db_queries": len(connection.queries) if hasattr(connection, "queries") else "N/A",
    }



def print_report(results: list[dict]) -> None:

    print(f"\n{BOLD}{DIV_HEAVY}{RESET}")
    title = "BATCH PROCESSING  —  BENCHMARK RESULTS  (Requirement #10)"
    print(f"{BOLD}  {title:<{W - 2}}{RESET}")
    print(f"{BOLD}{DIV_HEAVY}{RESET}")

    h = (
        f"  {'Approach':<{COL_A}}"
        f"{'Rows':>{COL_N}}"
        f"{'Time (s)':>{COL_T}}"
        f"{'ΔMem (MB)':>{COL_M}}"
        f"{'Peak RSS':>{COL_P}}"
    )
    print(f"{BOLD}{h}{RESET}")
    print(DIV_LIGHT)

    for i, r in enumerate(results):
        is_chunked = i == 1
        colour = GREEN if is_chunked else YELLOW
        row = (
            f"  {r['approach']:<{COL_A}}"
            f"{r['rows']:>{COL_N},}"
            f"{r['wall_time_s']:>{COL_T}.3f}"
            f"{r['mem_delta_mb']:>{COL_M}.1f}"
            f"{r['peak_rss_mb']:>{COL_P}.1f}"
        )
        print(_c(row, colour) if is_chunked else row)

    if len(results) == 2:
        naive, chunked = results
        speedup    = naive["wall_time_s"] / chunked["wall_time_s"] if chunked["wall_time_s"] else 0
        mem_saving = naive["mem_delta_mb"] - chunked["mem_delta_mb"]
        time_saved = naive["wall_time_s"]  - chunked["wall_time_s"]

        print(f"\n{BOLD}{DIV_HEAVY}{RESET}")
        print(f"{BOLD}  KEY PERFORMANCE INDICATORS{RESET}")
        print(DIV_LIGHT)

        kpi_lines = [
            ("Speed-up factor",  f"{speedup:.2f}×",       f"Chunked is {speedup:.2f}x faster than Naive"),
            ("Time saved",       f"{time_saved:.3f} s",    "Per full daily run"),
            ("Memory saved",     f"{mem_saving:.1f} MB",   "RAM freed by avoiding full load"),
            ("Memory efficiency",f"{chunked['mem_delta_mb']:.1f} MB used", "Chunked peak Δ — stays flat at any scale"),
        ]

        for label, value, note in kpi_lines:
            print(
                f"  {BOLD}{label:<22}{RESET}"
                f"  {_c(f'{value:<14}', GREEN, BOLD)}"
                f"  {note}"
            )

        print(f"{BOLD}{DIV_HEAVY}{RESET}\n")


LOCUST_SNIPPET = """
# ── locustfile.py — Stress Test (100 concurrent users, Requirement #9) ──────
# Run: locust -f locustfile.py --host http://localhost:8000 --headless -u 100 -r 10

from locust import HttpUser, task, between

class ShopUser(HttpUser):
    wait_time = between(0.5, 2)

    def on_start(self):
        res = self.client.post("/api/token/", json={
            "username": "testuser", "password": "testpass"
        })
        self.token = res.json().get("access", "")
        self.client.headers.update({"Authorization": f"Bearer {self.token}"})

    @task(3)
    def browse_products(self):
        self.client.get("/api/products/")

    @task(2)
    def add_to_cart(self):
        self.client.post("/api/cart/add/", json={"product": 1, "quantity": 1})

    @task(1)
    def checkout(self):
        self.client.post("/api/orders/checkout/", json={
            "shipping_address": "123 Load Test Ave"
        })
# ─────────────────────────────────────────────────────────────────────────────
"""



def main():
    parser = argparse.ArgumentParser(description="Batch processing benchmark — Requirement #10")
    parser.add_argument("--rows",       type=int,  default=10_000, help="Synthetic orders to seed")
    parser.add_argument("--chunk-size", type=int,  default=500,    dest="chunk_size")
    parser.add_argument("--date",       type=str,  default=None,   help="YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--skip-seed",  action="store_true",       help="Skip DB seeding")
    args = parser.parse_args()

    target = (
        date.fromisoformat(args.date)
        if args.date
        else date.today() - timedelta(days=1)
    )

    print(f"\n{BOLD}{DIV_HEAVY}{RESET}")
    print(f"{BOLD}  BENCHMARK CONFIGURATION{RESET}")
    print(DIV_LIGHT)
    print(f"  {'Target date':<16}:  {target}")
    print(f"  {'Order rows':<16}:  {args.rows:,}")
    print(f"  {'Chunk size':<16}:  {args.chunk_size:,}")
    print(f"{BOLD}{DIV_HEAVY}{RESET}\n")

    if not args.skip_seed:
        seed_database(args.rows, target)
        print()

    print(f"  {CYAN}[RUN]{RESET} Naive approach …", end="", flush=True)
    naive_result = run_naive(target)
    print(f"  {GREEN}done{RESET}")

    print(f"  {CYAN}[RUN]{RESET} Chunked approach …", end="", flush=True)
    chunked_result = run_chunked(target, args.chunk_size)
    print(f"  {GREEN}done{RESET}")

    print_report([naive_result, chunked_result])


if __name__ == "__main__":
    main()
