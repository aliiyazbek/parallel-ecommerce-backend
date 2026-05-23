import logging
import time
import traceback

from django.db import transaction

from notifications.models import AsyncTask
from notifications.tasks import TASK_REGISTRY

logger = logging.getLogger(__name__)


def process_one_batch(batch_size: int = 10) -> int:
    processed = 0

    # ── Synchronization point: atomically claim a batch of pending tasks ──────
    with transaction.atomic():
        # select_for_update(skip_locked=True): each worker locks only the rows
        # it claims and SKIPS rows already locked by another worker. This lets
        # several workers pull disjoint batches in parallel — no blocking, and
        # no task is ever processed twice (Requirement #3).
        tasks = (
            AsyncTask.objects
            .select_for_update(skip_locked=True)
            .filter(status=AsyncTask.STATUS_PENDING)
            .order_by("created_at")
            [:batch_size]
        )

        for task in tasks:
            task.mark_claimed()
            processed += 1

    # Run the handlers AFTER the claim transaction commits, so slow side-effects
    # never hold row locks while executing.
    for task in tasks:
        _execute_task(task)

    return processed


def _execute_task(task: AsyncTask) -> None:
    handler = TASK_REGISTRY.get(task.task_type)

    if handler is None:
        task.mark_failed(f"Unknown task_type: {task.task_type}")
        logger.error("[WORKER] Unknown task_type: %s (id=%s)", task.task_type, task.pk)
        return

    logger.info("[WORKER] Running | id=%s | type=%s", task.pk, task.task_type)
    try:
        handler(task.payload)
        task.mark_done()
        logger.info("[WORKER] Completed | id=%s", task.pk)
    except Exception as exc:
        error_detail = traceback.format_exc()
        task.mark_failed(error_detail)
        logger.warning(
            "[WORKER] Failed | id=%s | attempt=%s/%s | error=%s",
            task.pk, task.retries, task.max_retries, exc
        )


def run_forever(poll_interval: float = 2.0, batch_size: int = 10) -> None:
    logger.info(
        "[WORKER] Started | poll_interval=%.1fs | batch_size=%d",
        poll_interval, batch_size
    )
    while True:
        try:
            count = process_one_batch(batch_size=batch_size)
            if count == 0:
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            logger.info("[WORKER] Stopped manually")
            break
        except Exception as exc:
            logger.exception("[WORKER] Unexpected error: %s", exc)
            time.sleep(poll_interval)