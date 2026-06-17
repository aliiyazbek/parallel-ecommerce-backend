import functools
import logging
import threading
import time

# Aspect-Oriented Programming (AOP) layer: these decorators implement
# cross-cutting concerns — execution logging, latency timing, and audit
# trails — without polluting business logic. Applied as @log_execution and
# @audit_action on views, services, and batch jobs to monitor performance.
aop_logger = logging.getLogger("aop")
audit_logger = logging.getLogger("audit")
perf_logger = logging.getLogger("perf")


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


class PerfCollector:
    """Thread-safe sink for per-operation latency samples.

    This is the data side of the AOP performance aspect: the @measure decorator
    pushes one timing sample per call here, completely decoupled from business
    logic. A demo/report then asks the collector for aggregate stats (count,
    mean, p50/p95, error rate) to build the "before vs after" numbers — without
    a single timing statement living inside the services or views themselves.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._samples: dict[str, list[float]] = {}
        self._errors: dict[str, int] = {}

    def record(self, name: str, elapsed: float, *, error: bool = False) -> None:
        with self._lock:
            self._samples.setdefault(name, []).append(elapsed)
            if error:
                self._errors[name] = self._errors.get(name, 0) + 1

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._errors.clear()

    def stats(self, name: str) -> dict:
        import statistics
        with self._lock:
            samples = sorted(self._samples.get(name, []))
            errors = self._errors.get(name, 0)
        if not samples:
            return {"name": name, "count": 0, "errors": errors}
        n = len(samples)
        p = lambda q: samples[min(n - 1, int(n * q))]
        return {
            "name": name,
            "count": n,
            "errors": errors,
            "mean_ms": round(statistics.mean(samples) * 1000, 2),
            "p50_ms": round(statistics.median(samples) * 1000, 2),
            "p95_ms": round(p(0.95) * 1000, 2),
            "min_ms": round(samples[0] * 1000, 2),
            "max_ms": round(samples[-1] * 1000, 2),
        }


# Process-wide collector instance used by the @measure aspect and read back by
# the benchmark/demo commands.
perf = PerfCollector()


def measure(name: str | None = None, collector: PerfCollector | None = None):
    """AOP performance aspect: time a callable and push the sample to a collector.

    Cross-cutting — the wrapped function has no idea it is being measured. Used
    to capture the latency of the critical sections in the distributed-lock,
    load-distribution and ACID demos so the report layer can compute aggregate
    before/after metrics (Requirement #10 ties into this too).
    """
    sink = collector or perf

    def decorator(fn):
        op_name = name or getattr(fn, "__qualname__", fn.__name__)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            errored = False
            try:
                return fn(*args, **kwargs)
            except Exception:
                errored = True
                raise
            finally:
                elapsed = time.perf_counter() - t0
                sink.record(op_name, elapsed, error=errored)
                perf_logger.debug("[PERF] %s | %.3fs%s", op_name, elapsed,
                                  " | ERROR" if errored else "")

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
