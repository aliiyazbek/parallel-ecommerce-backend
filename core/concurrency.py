import functools
import threading

from rest_framework import status
from rest_framework.response import Response


class CapacityExceeded(Exception):
    pass


def limit_concurrency(max_concurrent: int, timeout: float = 0.0, on_reject=None):
    if max_concurrent < 1:
        raise ValueError("max_concurrent must be >= 1")

    # ── Synchronization point: bounded semaphore (bulkhead) ──────────────────
    # One shared counting semaphore guards entry to the wrapped callable, so at
    # most `max_concurrent` threads execute it simultaneously. This caps
    # parallelism and prevents resource exhaustion / oversubscription under
    # high concurrent load (Requirement #2 — Resource Management & Capacity).
    semaphore = threading.BoundedSemaphore(max_concurrent)

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            # Acquire a permit before entering the critical section. The blocking
            # form lets a caller wait up to `timeout`; otherwise we fail fast so
            # excess traffic is rejected instead of queueing without bound.
            if timeout > 0:
                acquired = semaphore.acquire(timeout=timeout)
            else:
                acquired = semaphore.acquire(blocking=False)
            if not acquired:
                if on_reject is not None:
                    return on_reject(*args, **kwargs)
                raise CapacityExceeded(
                    f"At capacity (max_concurrent={max_concurrent})"
                )
            try:
                return fn(*args, **kwargs)
            finally:
                # Release in `finally` so a permit is never leaked on error — a
                # leaked permit would permanently shrink the available capacity.
                semaphore.release()

        wrapper.semaphore = semaphore
        return wrapper

    return decorator


def drf_capacity_exceeded(*args, **kwargs):
    from rest_framework.response import Response
    from rest_framework import status

    return Response(
        {"detail": "Server at capacity. Please retry shortly."},
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )

