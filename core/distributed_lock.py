"""Distributed locking (Requirement #7 — Concurrency Control).

The existing in-process primitives in this project — `threading.Lock`,
`threading.BoundedSemaphore` and the DB-level `select_for_update()` —
coordinate threads *inside a single process* (or transactions inside a single
database). They are invisible to a *second* Python process or a *second*
application server.

Once we simulate Load Distribution (Requirement #5) the system is no longer one
process: several "servers" handle requests at the same time. An in-memory lock
held by Server A means nothing to Server B, so two servers can both read the
same stock, both pass the check and both decrement → the classic lost-update
race, now *across machines*.

A **distributed lock** solves this by keeping the "who holds the lock right
now" flag in a store that every server can see. We use **Redis** with the
canonical single-instance algorithm:

    SET <key> <token> NX PX <ttl>     # acquire: atomic "set if absent" + TTL
    <Lua compare-and-delete>          # release: only if the token is still ours

Properties this gives us:

* **Mutual exclusion** — `NX` ("set only if Not eXists") is atomic in Redis, so
  exactly one caller wins the key.
* **Deadlock freedom** — `PX <ttl>` auto-expires the key, so a crashed holder
  cannot freeze the resource forever.
* **Safe release** — each holder writes a unique random *token*; release runs a
  Lua script that deletes the key *only if* the stored token still matches.
  Without this, a holder whose lease already expired could delete a lock that a
  *different* holder has since acquired.

If Redis is not reachable we fall back to a process-local lock registry so the
demos still run on a machine with no Redis installed. The fallback is NOT a
real distributed lock (it only guards threads in one process) — it is clearly
flagged as such in `DistributedLock.backend` and in every report, so the
limitation is never hidden.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
import uuid

logger = logging.getLogger("locks")

# Redis client is optional at import time; we degrade gracefully if either the
# library or the server is missing.
try:
    import redis as _redis_lib
except ImportError:  # pragma: no cover - redis is in requirements, but be safe
    _redis_lib = None


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Lua: atomically delete the key ONLY if its value equals the token we hold.
# Running it as a single script makes the compare-and-delete atomic on the
# server, closing the "check then delete" race that a two-round-trip
# GET-then-DEL would open.
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

BACKEND_REDIS = "redis"
BACKEND_MEMORY = "memory (in-process fallback — NOT cross-process)"


# ── In-process fallback registry ────────────────────────────────────────────
# A single shared dict of named locks, guarded by one meta-lock. This mimics a
# lock service for a single process so the demo runs without Redis, while making
# it obvious (via the backend name) that it does not span processes.
_memory_locks: dict[str, threading.Lock] = {}
_memory_registry_guard = threading.Lock()


def _memory_lock_for(key: str) -> threading.Lock:
    with _memory_registry_guard:
        lock = _memory_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _memory_locks[key] = lock
        return lock


class _RedisState:
    """Lazily-created, process-wide Redis client + a cached liveness verdict."""

    client = None
    checked = False
    alive = False


def get_redis():
    """Return a live Redis client, or None if Redis is unavailable.

    The result is memoised: once we discover Redis is down we stop retrying on
    every lock acquisition (which would add a connection timeout to the hot
    path). Call `reset_redis_state()` in tests/demos to force a re-check.
    """
    if _RedisState.checked:
        return _RedisState.client if _RedisState.alive else None

    _RedisState.checked = True
    if _redis_lib is None:
        logger.warning("[lock] redis-py not installed → using in-process fallback")
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
    except Exception as exc:  # connection refused, timeout, auth, ...
        logger.warning(
            "[lock] Redis unreachable at %s (%s) → using in-process fallback",
            REDIS_URL, type(exc).__name__,
        )
        _RedisState.alive = False
        _RedisState.client = None
        return None

    logger.info("[lock] Redis OK at %s → distributed locking enabled", REDIS_URL)
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


class LockNotAcquired(Exception):
    """Raised by DistributedLock.__enter__ when the lock can't be taken in time."""


class DistributedLock:
    """A mutual-exclusion lock keyed by name, shared across processes via Redis.

    Usage::

        with DistributedLock(f"product:{pid}", ttl=5, blocking_timeout=3):
            ...critical section: read-check-write the shared resource...

    or non-blocking::

        lock = DistributedLock("foo", blocking=False)
        if lock.acquire():
            try: ...
            finally: lock.release()
    """

    def __init__(
        self,
        name: str,
        *,
        ttl: float = 10.0,
        blocking: bool = True,
        blocking_timeout: float = 5.0,
        retry_interval: float = 0.02,
        namespace: str = "lock",
    ):
        self.name = name
        self.key = f"{namespace}:{name}"
        self.ttl = ttl
        self.blocking = blocking
        self.blocking_timeout = blocking_timeout
        self.retry_interval = retry_interval

        # Unique fencing token: identifies THIS acquisition so release only ever
        # deletes a lock we still own.
        self.token = uuid.uuid4().hex
        self._held = False
        self._mem_lock: threading.Lock | None = None
        self.backend = active_backend()
        # Observability for the AOP / report layer:
        self.wait_seconds: float = 0.0
        self.contended: bool = False

    # ── acquire ──────────────────────────────────────────────────────────────
    def acquire(self) -> bool:
        client = get_redis()
        if client is not None:
            return self._acquire_redis(client)
        return self._acquire_memory()

    def _acquire_redis(self, client) -> bool:
        deadline = time.perf_counter() + self.blocking_timeout
        px = int(self.ttl * 1000)
        first = True
        t0 = time.perf_counter()
        while True:
            # SET key token NX PX ttl → True only if we created the key.
            ok = client.set(self.key, self.token, nx=True, px=px)
            if ok:
                self._held = True
                self.wait_seconds = time.perf_counter() - t0
                return True
            if first:
                # We lost the first attempt → someone else holds it = contention.
                self.contended = True
                first = False
            if not self.blocking or time.perf_counter() >= deadline:
                self.wait_seconds = time.perf_counter() - t0
                return False
            time.sleep(self.retry_interval)

    def _acquire_memory(self) -> bool:
        self._mem_lock = _memory_lock_for(self.key)
        t0 = time.perf_counter()
        if self.blocking:
            acquired = self._mem_lock.acquire(timeout=self.blocking_timeout)
        else:
            acquired = self._mem_lock.acquire(blocking=False)
        self.wait_seconds = time.perf_counter() - t0
        if acquired and self.wait_seconds > self.retry_interval:
            self.contended = True
        self._held = acquired
        return acquired

    # ── release ────────────────────────────────────────────────────────────--
    def release(self) -> None:
        if not self._held:
            return
        client = get_redis()
        if client is not None and self._mem_lock is None:
            # Compare-and-delete: only remove the key if our token is still there.
            try:
                client.eval(_RELEASE_LUA, 1, self.key, self.token)
            except Exception as exc:  # pragma: no cover
                logger.warning("[lock] release failed for %s: %s", self.key, exc)
        elif self._mem_lock is not None:
            with contextlib.suppress(RuntimeError):
                self._mem_lock.release()
        self._held = False

    # ── context manager ────────────────────────────────────────────────────--
    def __enter__(self) -> "DistributedLock":
        if not self.acquire():
            raise LockNotAcquired(
                f"could not acquire lock '{self.name}' within "
                f"{self.blocking_timeout}s (backend={self.backend})"
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
