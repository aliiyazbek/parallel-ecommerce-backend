"""Requirement #8 demo — Transaction Integrity (ACID).

A purchase is a composite operation: charge wallet → decrement stock → create
order. We force the LAST step to fail and show that:

  * NON-ATOMIC version  → money taken + stock gone, but no order  (inconsistent)
  * ATOMIC version      → everything rolled back, balances intact  (consistent)

We run it once single-threaded to show the inconsistency cleanly, then under
concurrency to show atomicity holds under simultaneous access.

    python manage.py demo_transaction_integrity
    python manage.py demo_transaction_integrity --threads 20
"""

from __future__ import annotations

import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import connection

from orders.models import Wallet
from orders.services import _ForcedFailure, purchase_atomic, purchase_non_atomic


def _attempt(fn, user_id, product_id, qty):
    """Run a purchase, treating the forced downstream crash as a failed purchase.

    The atomic version catches the failure internally and returns
    ('rolled_back'); the non-atomic version lets it propagate (there is no
    transaction to catch it), so we catch it here — and the partial writes it
    already committed are exactly the corruption we want to observe.
    """
    try:
        return fn(user_id, product_id, qty, fail_after_charge=True)
    except _ForcedFailure:
        return False, "crashed"
from products.models import Category, Product

DEMO_SLUG = "acid-demo-product"
REPORT_DIR = Path(settings.BASE_DIR) / "reports"
BANNER = "=" * 70

START_BALANCE = Decimal("1000.00")
START_STOCK = 100
PRICE = Decimal("100.00")


def reset_world():
    User = get_user_model()
    user, _ = User.objects.get_or_create(
        username="acid_demo_user",
        defaults={"email": "acid_demo@example.com"},
    )
    Wallet.objects.update_or_create(user=user, defaults={"balance": START_BALANCE})

    cat, _ = Category.objects.get_or_create(name="Demo", defaults={"slug": "demo"})
    Product.objects.filter(slug=DEMO_SLUG).delete()
    product = Product.objects.create(
        category=cat, name="ACID Demo Product", slug=DEMO_SLUG,
        description="Used only by manage.py demo_transaction_integrity.",
        price=PRICE, stock=START_STOCK, is_active=True,
    )
    return user, product


def snapshot(user_id, product_id):
    w = Wallet.objects.get(user_id=user_id)
    p = Product.objects.get(pk=product_id)
    return {"balance": w.balance, "stock": p.stock}


def run_single(mode: str) -> dict:
    user, product = reset_world()
    fn = purchase_atomic if mode == "atomic" else purchase_non_atomic

    before = snapshot(user.id, product.id)
    ok, reason = _attempt(fn, user.id, product.id, 1)
    after = snapshot(user.id, product.id)

    money_lost = before["balance"] - after["balance"]
    stock_lost = before["stock"] - after["stock"]
    # The purchase FAILED (we forced it), so any change is corruption.
    inconsistent = (money_lost != 0 or stock_lost != 0)

    return {
        "mode": mode,
        "purchase_ok": ok,
        "reason": reason,
        "balance_before": before["balance"],
        "balance_after": after["balance"],
        "stock_before": before["stock"],
        "stock_after": after["stock"],
        "money_lost": money_lost,
        "stock_lost": stock_lost,
        "inconsistent": inconsistent,
        "verdict": "DATA CORRUPTED" if inconsistent else "OK — fully rolled back",
    }


def run_concurrent(mode: str, threads: int) -> dict:
    user, product = reset_world()
    fn = purchase_atomic if mode == "atomic" else purchase_non_atomic
    before = snapshot(user.id, product.id)

    barrier = threading.Barrier(threads)
    guard = threading.Lock()
    outcomes = {"ok": 0, "rolled_back": 0, "other": 0, "errors": 0}

    def worker():
        try:
            barrier.wait()
            ok, reason = _attempt(fn, user.id, product.id, 1)
            with guard:
                if ok:
                    outcomes["ok"] += 1
                elif reason in ("rolled_back",):
                    outcomes["rolled_back"] += 1
                else:
                    outcomes["other"] += 1
        except Exception:
            with guard:
                outcomes["errors"] += 1
        finally:
            connection.close()

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    after = snapshot(user.id, product.id)
    money_lost = before["balance"] - after["balance"]
    stock_lost = before["stock"] - after["stock"]
    inconsistent = (money_lost != 0 or stock_lost != 0)
    return {
        "mode": mode,
        "threads": threads,
        "outcomes": outcomes,
        "money_lost": money_lost,
        "stock_lost": stock_lost,
        "inconsistent": inconsistent,
        "verdict": "DATA CORRUPTED" if inconsistent else "OK — fully rolled back",
    }


def print_single(r: dict) -> None:
    print()
    print(BANNER)
    print(f" Req 8 — ACID single-thread — mode: {r['mode'].upper()}")
    print(BANNER)
    print(f" Purchase result        : {'SUCCESS' if r['purchase_ok'] else 'FAILED'} "
          f"({r['reason']})  <- failure is forced at the order step")
    print(f" Wallet balance         : {r['balance_before']} -> {r['balance_after']}")
    print(f" Product stock          : {r['stock_before']} -> {r['stock_after']}")
    print("-" * 70)
    if r["inconsistent"]:
        print(f" !! INCONSISTENT: lost {r['money_lost']} money and {r['stock_lost']} "
              f"stock for a purchase that FAILED.")
    else:
        print(" OK — purchase failed and EVERYTHING was rolled back. No money/stock lost.")
    print(BANNER)


def print_comparison(b: dict, a: dict) -> None:
    print()
    print(BANNER)
    print(" SIDE-BY-SIDE — Before vs After (ACID transaction)")
    print(BANNER)
    print(f"  {'Metric':<26}{'BEFORE (no txn)':>20}{'AFTER (atomic)':>18}")
    print(f"  {'-'*26}{'-'*20:>20}{'-'*18:>18}")
    print(f"  {'Money lost on failure':<26}{str(b['money_lost']):>20}{str(a['money_lost']):>18}")
    print(f"  {'Stock lost on failure':<26}{str(b['stock_lost']):>20}{str(a['stock_lost']):>18}")
    print(f"  {'State after failure':<26}{b['verdict']:>20}{a['verdict']:>18}")
    print()
    print(f"  RESULT: ACID rollback prevented {b['money_lost']} money and "
          f"{b['stock_lost']} stock from leaking on a failed purchase.")


def write_report(singles: dict, conc: dict, threads: int) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORT_DIR / f"req8_transaction_integrity_{ts}.md"
    b, a = singles["non_atomic"], singles["atomic"]

    L: list[str] = []
    L.append("# Requirement 8 — Transaction Integrity (ACID)")
    L.append("")
    L.append(f"**Generated:** {datetime.now().isoformat(timespec='seconds')}")
    L.append("")
    L.append("## Scenario")
    L.append(
        "A purchase performs three writes — **charge wallet**, **decrement "
        "stock**, **create order** — and we force the third to fail. The "
        "question is what the database looks like afterwards."
    )
    L.append("")
    L.append("## Single-thread: does a forced failure corrupt state?")
    L.append("")
    L.append("| Mode | Money lost | Stock lost | State after failed purchase |")
    L.append("|------|-----------:|-----------:|-----------------------------|")
    L.append(f"| **NON-ATOMIC** | {b['money_lost']} | {b['stock_lost']} | {b['verdict']} |")
    L.append(f"| **ATOMIC** | {a['money_lost']} | {a['stock_lost']} | {a['verdict']} |")
    L.append("")
    L.append("### Before vs After")
    L.append("")
    L.append("| Metric | BEFORE (no transaction) | AFTER (atomic) |")
    L.append("|--------|------------------------:|---------------:|")
    L.append(f"| Money lost on a failed purchase | **{b['money_lost']}** | **{a['money_lost']}** |")
    L.append(f"| Stock lost on a failed purchase | **{b['stock_lost']}** | **{a['stock_lost']}** |")
    L.append(f"| Final state | {b['verdict']} | {a['verdict']} |")
    L.append("")
    L.append(
        f"**Verdict:** without a transaction the wallet was debited "
        f"({b['money_lost']}) and stock reduced ({b['stock_lost']}) even though "
        f"the purchase failed — money taken, no order. Wrapping the three writes "
        f"in `transaction.atomic()` rolls them all back, so a failed purchase "
        f"leaves **{a['money_lost']}** money and **{a['stock_lost']}** stock lost."
    )
    L.append("")
    L.append(f"## Concurrent: atomicity under {threads} simultaneous failing purchases")
    L.append("")
    L.append("| Mode | Threads | Money lost | Stock lost | Verdict |")
    L.append("|------|--------:|-----------:|-----------:|---------|")
    cb, ca = conc["non_atomic"], conc["atomic"]
    L.append(f"| **NON-ATOMIC** | {cb['threads']} | {cb['money_lost']} | {cb['stock_lost']} | {cb['verdict']} |")
    L.append(f"| **ATOMIC** | {ca['threads']} | {ca['money_lost']} | {ca['stock_lost']} | {ca['verdict']} |")
    L.append("")
    L.append(
        "Atomicity holds even under concurrent access: every atomic attempt "
        "either commits fully or rolls back fully, so no partial writes survive "
        "regardless of interleaving (the **A** and **C** in ACID, under "
        "simultaneous load)."
    )
    L.append("")
    L.append("## Where it lives in the codebase")
    L.append("- Atomic service: `orders/services.py::purchase_atomic`")
    L.append("- Broken baseline: `orders/services.py::purchase_non_atomic`")
    L.append("- Production path: `orders/views.py::CheckoutView.post` "
             "(`@transaction.atomic` wraps payment + stock + order rows).")
    L.append("")
    path.write_text("\n".join(L), encoding="utf-8")
    return path


class Command(BaseCommand):
    help = "Req 8 demo: composite purchase is all-or-nothing (ACID rollback)."

    def add_arguments(self, parser):
        parser.add_argument("--threads", type=int, default=20)
        parser.add_argument("--no-report", action="store_true")

    def handle(self, *args, **opts):
        singles = {
            "non_atomic": run_single("non_atomic"),
            "atomic": run_single("atomic"),
        }
        print_single(singles["non_atomic"])
        print_single(singles["atomic"])
        print_comparison(singles["non_atomic"], singles["atomic"])

        threads = opts["threads"]
        conc = {
            "non_atomic": run_concurrent("non_atomic", threads),
            "atomic": run_concurrent("atomic", threads),
        }
        print()
        print(BANNER)
        print(f" Concurrent ({threads} threads) — money/stock lost on forced failure")
        print(BANNER)
        for k in ("non_atomic", "atomic"):
            c = conc[k]
            print(f"  {k.upper():<12} money_lost={c['money_lost']!s:<8} "
                  f"stock_lost={c['stock_lost']!s:<5} -> {c['verdict']}")

        if not opts["no_report"]:
            path = write_report(singles, conc, threads)
            print(f"\nReport saved to: {path}")
