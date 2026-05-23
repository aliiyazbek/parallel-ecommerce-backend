import time

from django.db import transaction

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
