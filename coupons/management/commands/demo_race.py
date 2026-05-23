import threading
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

from coupons.models import Coupon
from coupons.services import redeem_safe, redeem_unsafe


DEMO_CODE = "RACE-DEMO"
REPORT_DIR = Path(settings.BASE_DIR) / "reports"


def reset_coupon(max_uses: int) -> Coupon:
    Coupon.objects.filter(code=DEMO_CODE).delete()
    return Coupon.objects.create(
        code=DEMO_CODE,
        discount_percent=50,
        max_uses=max_uses,
        usage_count=0,
        is_active=True,
    )


def run_scenario(mode: str, threads: int, max_uses: int) -> dict:
    reset_coupon(max_uses)
    fn = redeem_safe if mode == "safe" else redeem_unsafe

    successes: list[int] = []
    failures: list[int] = []
    errors: list[str] = []
    lock = threading.Lock()

    barrier = threading.Barrier(threads)

    def worker(i: int):
        try:
            barrier.wait()
            ok, reason = fn(DEMO_CODE)
            with lock:
                (successes if ok else failures).append(i)
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

    coupon = Coupon.objects.get(code=DEMO_CODE)
    oversold = max(0, len(successes) - max_uses)

    print()
    print("=" * 60)
    print(f" Race Condition Demo — mode: {mode.upper()}")
    print("=" * 60)
    print(f" Threads:           {threads}")
    print(f" max_uses:          {max_uses}")
    print(f" Successes:         {len(successes)}")
    print(f" Failures:          {len(failures)}")
    print(f" IntegrityErrors:   {len(errors)}")
    print(f" Final usage_count: {coupon.usage_count}")
    print("-" * 60)
    if oversold:
        print(f" !! OVERSOLD by {oversold} (bug reproduced)")
    else:
        print(" OK — no overselling, limit respected")
    if errors:
        print(" Sample errors:")
        for e in errors[:3]:
            print(f"   - {e}")
    print("=" * 60)

    return {
        "mode": mode,
        "threads": threads,
        "max_uses": max_uses,
        "successes": len(successes),
        "failures": len(failures),
        "errors": len(errors),
        "final_usage_count": coupon.usage_count,
        "oversold_by": oversold,
        "verdict": "BUG REPRODUCED" if oversold else "OK — limit respected",
    }


def write_report(results: list[dict]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORT_DIR / f"task1_coupon_race_{ts}.md"

    lines: list[str] = []
    lines.append("# Task 1 (extra) — Race Condition on Coupon Redemption")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now().isoformat(timespec='seconds')}")
    lines.append("")
    lines.append(
        "This is the secondary Task-1 demo (the primary one is on product "
        "stock — see `reports/task1_stock_race_*.md`). The same fix applies: "
        "wrap the read-modify-write in a transaction and hold a row-level "
        "lock with `SELECT ... FOR UPDATE`."
    )
    lines.append("")
    lines.append("| Mode | Threads | max_uses | Successes | Failures | Final usage_count | Oversold | Verdict |")
    lines.append("|------|--------:|---------:|----------:|---------:|------------------:|---------:|---------|")
    for r in results:
        lines.append(
            f"| **{r['mode'].upper()}** | {r['threads']} | {r['max_uses']} | "
            f"{r['successes']} | {r['failures']} | {r['final_usage_count']} | "
            f"{r['oversold_by']} | {r['verdict']} |"
        )
    lines.append("")
    lines.append("## Where the fix lives")
    lines.append("- Safe: `coupons/services.py::redeem_safe`")
    lines.append("- Unsafe (demo only): `coupons/services.py::redeem_unsafe`")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class Command(BaseCommand):
    help = "Demonstrate the coupon redemption race condition (and its fix)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--mode",
            choices=["unsafe", "safe", "both"],
            default="both",
        )
        parser.add_argument("--threads", type=int, default=50)
        parser.add_argument("--max-uses", type=int, default=10)
        parser.add_argument("--no-report", action="store_true")

    def handle(self, *args, **opts):
        modes = ["unsafe", "safe"] if opts["mode"] == "both" else [opts["mode"]]
        results = [run_scenario(m, opts["threads"], opts["max_uses"]) for m in modes]
        if not opts["no_report"]:
            path = write_report(results)
            print(f"\nReport saved to: {path}")
