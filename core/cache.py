"""Distributed caching (Requirement #6 — Distributed Caching).

Every read of a popular product currently hits the database. Under load that
means thousands of identical `SELECT`s for the same few rows — the database
becomes the bottleneck even though the answer barely changes.

A **distributed cache** keeps those hot rows in a fast store that *every*
application node shares. We use **Redis** with the canonical **cache-aside**
(lazy-loading) pattern:

    READ:
        value = cache.get(key)
        if value is None:            # MISS  → pay for the DB query once...
            value = query_database()
            cache.set(key, value, ttl)   # ...then remember it for next time
        return value                 # HIT   → no DB query at all

    WRITE (product updated/deleted):
        cache.delete(key)            # invalidate so the next read reloads

Why *distributed* (Redis) and not a per-process dict:

* Under Load Distribution (Req #5) there are several application nodes. A local
  dict in node A is invisible to node B, so each node would keep its own stale
  copy and an update on one node would not invalidate the others. A shared Redis
  cache gives every node ONE consistent view and ONE place to invalidate.

If Redis is not reachable we fall back to a process-local dict so the demo still
runs without Redis installed. The fallback is NOT a real distributed cache (it
only serves one process) — it is clearly flagged in `active_backend()` and in
every report, so the limitation is never hidden.

All cache operations are wrapped by the AOP `@measure` aspect (see
`core/aop.py`), so the demo/report layer can read hit/miss latency and counts
without a single timing statement living inside the cache logic itself.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from core.aop import measure

logger = logging.getLogger("cache")

# Reuse the same optional-redis strategy as the lock layer: degrade gracefully if
# the library or the server is missing.
try:
    import redis as _redis_lib
except ImportError:  # pragma: no cover - redis is in requirements, but be safe
    _redis_lib = None

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

BACKEND_REDIS = "redis"
BACKEND_MEMORY = "memory (in-process fallback — NOT distributed)"

# Counters the AOP/report layer reads to compute the cache hit ratio. Guarded by
# a lock so concurrent readers in the stress demo don't lose increments.
_stats_lock = threading.Lock()
_stats = {"hits": 0, "misses": 0, "sets": 0, "invalidations": 0}


def _bump(field: str, n: int = 1) -> None:
    with _stats_lock:
        _stats[field] += n


def cache_stats() -> dict:
    """Snapshot of hit/miss counters plus the derived hit ratio (0..1)."""
    with _stats_lock:
        snap = dict(_stats)
    total = snap["hits"] + snap["misses"]
    snap["lookups"] = total
    snap["hit_ratio"] = (snap["hits"] / total) if total else 0.0
    return snap


def reset_cache_stats() -> None:
    with _stats_lock:
        for k in _stats:
            _stats[k] = 0


# ── In-process fallback store ───────────────────────────────────────────────
# A single shared dict (value, expires_at) guarded by one lock. Mimics a cache
# for ONE process so the demo runs without Redis, while the backend name makes
# it obvious it is not shared across processes.
_memory_store: dict[str, tuple[str, float]] = {}
_memory_guard = threading.Lock()


class _RedisState:
    client = None
    checked = False
    alive = False


def get_redis():
    """Return a live Redis client, or None if Redis is unavailable (memoised)."""
    if _RedisState.checked:
        return _RedisState.client if _RedisState.alive else None

    _RedisState.checked = True
    if _redis_lib is None:
        logger.warning("[cache] redis-py not installed → in-process fallback")
        _RedisState.alive = False
        return None
    try:
        client = _redis_lib.Redis.from_url(
            REDIS_URL,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
            decode_responses=True,
        )
        client.ping()
    except Exception as exc:
        logger.warning(
            "[cache] Redis unreachable at %s (%s) → in-process fallback",
            REDIS_URL, type(exc).__name__,
        )
        _RedisState.alive = False
        _RedisState.client = None
        return None

    logger.info("[cache] Redis OK at %s → distributed cache enabled", REDIS_URL)
    _RedisState.client = client
    _RedisState.alive = True
    return client


def reset_redis_state() -> None:
    """Force the next get_redis() to re-probe the server (used by demos)."""
    _RedisState.client = None
    _RedisState.checked = False
    _RedisState.alive = False


def active_backend() -> str:
    return BACKEND_REDIS if get_redis() is not None else BACKEND_MEMORY


# ── Cache operations (AOP-instrumented) ─────────────────────────────────────
# Each op is decorated with @measure so its latency is recorded by the perf
# collector. The hit/miss counters are bumped here too, so the report layer can
# read everything from the AOP/stats side without touching business code.

@measure("cache.get")
def cache_get(key: str):
    """Return the cached raw string for `key`, or None on a miss."""
    client = get_redis()
    if client is not None:
        value = client.get(key)
    else:
        with _memory_guard:
            entry = _memory_store.get(key)
            if entry is not None and entry[1] >= time.time():
                value = entry[0]
            else:
                if entry is not None:
                    _memory_store.pop(key, None)  # expired
                value = None

    if value is None:
        _bump("misses")
    else:
        _bump("hits")
    return value


@measure("cache.set")
def cache_set(key: str, value: str, ttl: float = 60.0) -> None:
    """Store `value` under `key` for `ttl` seconds."""
    client = get_redis()
    if client is not None:
        client.set(key, value, px=int(ttl * 1000))
    else:
        with _memory_guard:
            _memory_store[key] = (value, time.time() + ttl)
    _bump("sets")


@measure("cache.delete")
def cache_delete(key: str) -> None:
    """Invalidate `key` so the next read reloads from the source of truth."""
    client = get_redis()
    if client is not None:
        client.delete(key)
    else:
        with _memory_guard:
            _memory_store.pop(key, None)
    _bump("invalidations")


def clear_all() -> None:
    """Drop every cached entry (used by demos to start from a cold cache)."""
    client = get_redis()
    if client is not None:
        try:
            client.flushdb()
        except Exception as exc:  # pragma: no cover
            logger.warning("[cache] flushdb failed: %s", exc)
    with _memory_guard:
        _memory_store.clear()
