import time
from django.db import transaction
from .models import Coupon

RACE_WINDOW_SECONDS = 0.05


def redeem_unsafe(code: str) -> tuple[bool, str]:
    # UNSAFE baseline (NO synchronization) — kept to demonstrate the race.
    # Concurrent redemptions can both read the same usage_count, both pass the
    # max_uses check, and both increment — letting a coupon exceed its limit
    # (lost-update race condition).
    try:
        coupon = Coupon.objects.get(code=code)
    except Coupon.DoesNotExist:
        return False, "invalid_code"

    if not coupon.is_active:
        return False, "inactive"
    if coupon.usage_count >= coupon.max_uses:
        return False, "exhausted"

    time.sleep(RACE_WINDOW_SECONDS)  # widen the read→write window to expose the race

    coupon.usage_count += 1
    coupon.save(update_fields=["usage_count"])
    return True, "redeemed"


def redeem_safe(code: str) -> tuple[bool, str]:
    # ── Synchronization point: pessimistic row lock inside a transaction ──────
    with transaction.atomic():
        # SELECT ... FOR UPDATE locks this coupon row until commit, so concurrent
        # redemptions are serialized and the max_uses limit is enforced exactly
        # (no over-redemption) even under simultaneous access (Req #1 & #7).
        try:
            coupon = Coupon.objects.select_for_update().get(code=code)
        except Coupon.DoesNotExist:
            return False, "invalid_code"

        if not coupon.is_active:
            return False, "inactive"
        if coupon.usage_count >= coupon.max_uses:
            return False, "exhausted"

        time.sleep(RACE_WINDOW_SECONDS)  # identical delay to the unsafe version, but harmless under the lock

        coupon.usage_count += 1
        coupon.save(update_fields=["usage_count"])
        return True, "redeemed"
