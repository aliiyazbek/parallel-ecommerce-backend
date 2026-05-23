import functools
import logging
import time

# Aspect-Oriented Programming (AOP) layer: these decorators implement
# cross-cutting concerns — execution logging, latency timing, and audit
# trails — without polluting business logic. Applied as @log_execution and
# @audit_action on views, services, and batch jobs to monitor performance.
aop_logger = logging.getLogger("aop")
audit_logger = logging.getLogger("audit")


def _is_successful(result) -> bool:
    status_code = getattr(result, "status_code", None)
    if status_code is None:
        return True
    return 200 <= status_code < 300


def log_execution(level: int = logging.INFO, logger: logging.Logger | None = None):
    log = logger or aop_logger

    def decorator(fn):
        qualname = getattr(fn, "__qualname__", fn.__name__)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            log.log(level, "[AOP] enter  | %s", qualname)
            t0 = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                elapsed = time.perf_counter() - t0
                log.log(
                    logging.ERROR,
                    "[AOP] raised | %s | %.3fs | %s: %s",
                    qualname, elapsed, type(exc).__name__, exc,
                )
                raise
            elapsed = time.perf_counter() - t0
            log.log(level, "[AOP] exit   | %s | %.3fs", qualname, elapsed)
            return result

        return wrapper

    return decorator


def audit_action(action: str, extract=None):

    def decorator(fn):
        qualname = getattr(fn, "__qualname__", fn.__name__)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)
            if not _is_successful(result):
                return result
            try:
                context = extract(result, *args, **kwargs) if extract else {}
            except Exception as exc:
                context = {"_extract_error": f"{type(exc).__name__}: {exc}"}
            audit_logger.info("[AUDIT] %s | %s | %s", action, qualname, context)
            return result

        return wrapper

    return decorator
