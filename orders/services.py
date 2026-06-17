import time
from decimal import Decimal

from django.db import transaction

from core.aop import measure
from core.distributed_lock import DistributedLock, LockNotAcquired
from products.models import Product

RACE_WINDOW_SECONDS = 0.05


def decrement_stock_unsafe(product_id: int, qty: int = 1) -> tuple[bool, str]:
    # UNSAFE baseline (NO synchronization) — kept to demonstrate the race.
    # The read → check → write below is not atomic: two concurrent threads can
    # both read the same stock, both pass the check, and both write — so one
    # decrement is lost (the classic lost-update race condition).
    try:
        product = Product.objects.get(pk=product_id)
    except Product.DoesNotExist:
        return False, "not_found"

    if product.stock < qty:
        return False, "out_of_stock"

    time.sleep(RACE_WINDOW_SECONDS)  # widen the read→write window to expose the race

    product.stock -= qty
    product.save(update_fields=["stock"])
    return True, "ok"


def decrement_stock_safe(product_id: int, qty: int = 1) -> tuple[bool, str]:
    # ── Synchronization point: pessimistic row lock inside a transaction ──────
    with transaction.atomic():
        # SELECT ... FOR UPDATE locks this product row until the transaction
        # commits; any concurrent transaction touching the same row blocks here.
        # That serializes the read-check-write and removes the race (Req #1 & #7).
        try:
            product = Product.objects.select_for_update().get(pk=product_id)
        except Product.DoesNotExist:
            return False, "not_found"

        if product.stock < qty:
            return False, "out_of_stock"

        time.sleep(RACE_WINDOW_SECONDS)  # identical delay to the unsafe version, but harmless under the lock

        product.stock -= qty
        product.save(update_fields=["stock"])
        return True, "ok"


# ─────────────────────────────────────────────────────────────────────────────
# Requirement #7 — Concurrency Control with a DISTRIBUTED lock.
#
# decrement_stock_unsafe / _safe above protect ONE process (DB row lock). Once
# Requirement #5 puts several servers behind a load balancer, the read-modify-
# write of an in-memory cached value, or any logic that must be exclusive across
# *processes*, is no longer covered. The functions below take a cross-process
# DistributedLock (Redis) around the critical section instead.
#
# To make the danger visible we deliberately operate on a value that an ORM row
# lock would NOT serialize on its own: we read the stock, hold it in a Python
# variable across a delay (as an app-server cache would), then write it back.
# Without the distributed lock, two servers interleave and oversell.
# ─────────────────────────────────────────────────────────────────────────────

@measure("stock.dlock_unsafe")
def decrement_stock_dlock_unsafe(product_id: int, qty: int = 1) -> tuple[bool, str]:
    # UNSAFE baseline — NO distributed lock. Models app-level read-check-write
    # (e.g. operating on a cached count) that crosses processes. Two servers can
    # both read the same value and both write → lost update across servers.
    try:
        product = Product.objects.get(pk=product_id)
    except Product.DoesNotExist:
        return False, "not_found"

    current = product.stock           # read (think: from cache / a prior query)
    if current < qty:
        return False, "out_of_stock"

    time.sleep(RACE_WINDOW_SECONDS)   # interleaving window across servers

    product.stock = current - qty     # write back the value we computed earlier
    product.save(update_fields=["stock"])
    return True, "ok"


@measure("stock.dlock_safe")
def decrement_stock_dlock_safe(product_id: int, qty: int = 1) -> tuple[bool, str]:
    # ── Synchronization point: cross-process distributed lock (Redis) ─────────
    # Only one server anywhere may be inside this block for a given product at a
    # time. The same read-check-write that oversold above is now serialized
    # across ALL servers, so the stock limit holds even under load distribution.
    try:
        with DistributedLock(f"stock:{product_id}", ttl=5, blocking_timeout=5):
            try:
                product = Product.objects.get(pk=product_id)
            except Product.DoesNotExist:
                return False, "not_found"

            current = product.stock
            if current < qty:
                return False, "out_of_stock"

            time.sleep(RACE_WINDOW_SECONDS)   # identical delay; harmless under the lock

            product.stock = current - qty
            product.save(update_fields=["stock"])
            return True, "ok"
    except LockNotAcquired:
        return False, "lock_timeout"


# ─────────────────────────────────────────────────────────────────────────────
# Requirement #8 — Transaction Integrity (ACID).
#
# A purchase is a COMPOSITE operation: (1) charge the wallet, (2) decrement
# stock, (3) create the order. It must be all-or-nothing: if any step fails,
# none of the earlier side-effects may survive. We force a failure in step (3)
# to prove the rollback.
# ─────────────────────────────────────────────────────────────────────────────

class _ForcedFailure(Exception):
    """Simulates a downstream failure (e.g. order-service crash) after charging."""


def purchase_non_atomic(user_id: int, product_id: int, qty: int,
                        fail_after_charge: bool = True) -> tuple[bool, str]:
    # BROKEN baseline — NO transaction. Each step commits on its own. If step 3
    # fails, the wallet was already debited and stock already reduced → money
    # taken, no order. The database is left in an inconsistent state.
    from orders.models import Order, Wallet

    wallet = Wallet.objects.get(user_id=user_id)
    product = Product.objects.get(pk=product_id)
    price = product.price * qty

    if wallet.balance < price or product.stock < qty:
        return False, "insufficient"

    wallet.balance = wallet.balance - price          # step 1: charge (commits)
    wallet.save(update_fields=["balance"])

    product.stock = product.stock - qty              # step 2: stock (commits)
    product.save(update_fields=["stock"])

    if fail_after_charge:
        raise _ForcedFailure("order service crashed after charge")  # step 3 fails

    Order.objects.create(user_id=user_id, shipping_address="demo", total=price)
    return True, "ok"


def purchase_atomic(user_id: int, product_id: int, qty: int,
                    fail_after_charge: bool = True) -> tuple[bool, str]:
    # ── Synchronization point: atomic transaction = all-or-nothing (ACID) ─────
    # All three writes share one transaction. The forced failure raises, the
    # transaction rolls back, and NEITHER the wallet debit NOR the stock change
    # is persisted — Atomicity + Consistency preserved even under concurrency.
    from orders.models import Order, Wallet

    try:
        with transaction.atomic():
            wallet = Wallet.objects.select_for_update().get(user_id=user_id)
            product = Product.objects.select_for_update().get(pk=product_id)
            price = product.price * qty

            if wallet.balance < price or product.stock < qty:
                return False, "insufficient"

            wallet.balance = wallet.balance - price
            wallet.save(update_fields=["balance"])

            product.stock = product.stock - qty
            product.save(update_fields=["stock"])

            if fail_after_charge:
                raise _ForcedFailure("order service crashed after charge")

            Order.objects.create(user_id=user_id, shipping_address="demo", total=price)
            return True, "ok"
    except _ForcedFailure:
        return False, "rolled_back"
