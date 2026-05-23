import logging
import time

from core.aop import log_execution

logger = logging.getLogger(__name__)


@log_execution()
def send_invoice_email(payload: dict) -> None:
    order_id   = payload["order_id"]
    user_email = payload["user_email"]
    total      = payload["total"]

    logger.info("[INVOICE] Starting | order_id=%s | email=%s", order_id, user_email)

    time.sleep(0.5)

    logger.info("[INVOICE] Done | order_id=%s | total=%s | sent to=%s", order_id, total, user_email)


@log_execution()
def send_order_notification(payload: dict) -> None:
    order_id   = payload["order_id"]
    username   = payload["username"]
    item_count = payload.get("item_count", "?")

    logger.info("[NOTIFY] Starting | order_id=%s | user=%s | items=%s", order_id, username, item_count)

    time.sleep(0.3)

    logger.info("[NOTIFY] Done | order_id=%s | user=%s", order_id, username)



TASK_REGISTRY = {
    "send_invoice":      send_invoice_email,
    "send_notification": send_order_notification,
}