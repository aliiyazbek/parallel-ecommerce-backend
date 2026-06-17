"""Load Distribution simulation (Requirement #5).

We model a horizontally-scaled deployment: instead of one backend process, a
*pool of N identical "servers"* sits behind a **load balancer**. Each incoming
request is handed to one server according to a balancing **strategy**.

Why this matters for a Parallel Programming project
---------------------------------------------------
* It is the scenario that makes Requirement #7 (a *distributed* lock) necessary:
  with several servers, an in-process lock no longer protects shared state, so
  the servers must coordinate through Redis.
* It lets us reason quantitatively about strategy choice — the report compares
  the per-server load distribution and the tail latency of each strategy and
  *justifies* the one we adopt (Requirement #5 asks explicitly for the strategy
  to be explained and justified).

Each "server" is a `BackendServer` with a fixed worker capacity (a bounded
semaphore) so it can only process `capacity` requests at once — exactly like a
real worker/thread pool. We do NOT spin up OS processes or sockets: the focus
of the course is the *concurrency behaviour*, which a worker-pool-per-server
model reproduces faithfully and deterministically.

Strategies implemented
-----------------------
* ``round_robin``        — server i, i+1, i+2 … cycling. Simple, stateless,
                           great when requests cost roughly the same.
* ``random``             — pick a server uniformly at random. Stateless and
                           lock-free, but can transiently imbalance.
* ``least_connections``  — send to the server with the fewest in-flight
                           requests right now. Adapts to uneven request cost;
                           needs a tiny bit of shared state (an atomic counter
                           per server), which is itself a synchronization point.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class BackendServer:
    """One simulated application server with a finite worker pool."""

    name: str
    capacity: int = 4
    # Per-request processing cost in seconds (the simulated "work").
    base_latency: float = 0.02

    # ── runtime counters (all touched from many threads → guarded) ──
    handled: int = 0
    rejected: int = 0
    max_inflight: int = 0
    _inflight: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _pool: threading.BoundedSemaphore = field(init=False, repr=False)

    def __post_init__(self):
        self._pool = threading.BoundedSemaphore(self.capacity)

    @property
    def inflight(self) -> int:
        with self._lock:
            return self._inflight

    def handle(self, work=None) -> bool:
        """Process one request. Returns False if the server is saturated.

        `work` is an optional zero-arg callable run inside the server's worker
        slot — this is where the demo plugs in the real critical section (e.g. a
        stock decrement under a distributed lock).
        """
        # ── Synchronization point: admission to this server's worker pool ──
        if not self._pool.acquire(blocking=False):
            with self._lock:
                self.rejected += 1
            return False
        try:
            with self._lock:
                self._inflight += 1
                self.handled += 1
                if self._inflight > self.max_inflight:
                    self.max_inflight = self._inflight
            if work is not None:
                work()
            else:
                time.sleep(self.base_latency)
            return True
        finally:
            with self._lock:
                self._inflight -= 1
            self._pool.release()


class LoadBalancer:
    """Routes requests across a pool of BackendServers by a chosen strategy."""

    STRATEGIES = ("round_robin", "random", "least_connections")

    def __init__(self, servers: list[BackendServer], strategy: str = "round_robin"):
        if strategy not in self.STRATEGIES:
            raise ValueError(f"unknown strategy {strategy!r}")
        self.servers = servers
        self.strategy = strategy
        self._rr_index = 0
        self._rr_lock = threading.Lock()
        self.unserved = 0  # requests no server could take (all saturated)
        self._unserved_lock = threading.Lock()

    # ── strategy implementations ────────────────────────────────────────────
    def _pick_round_robin(self) -> BackendServer:
        # The index is shared mutable state across router threads → guard it so
        # two threads don't grab the same slot and skew the distribution.
        with self._rr_lock:
            srv = self.servers[self._rr_index % len(self.servers)]
            self._rr_index += 1
        return srv

    def _pick_random(self, seed: int) -> BackendServer:
        # Deterministic pseudo-random without Math.random: mix the per-request
        # seed so the choice is reproducible across runs.
        idx = (seed * 2654435761) % len(self.servers)
        return self.servers[idx]

    def _pick_least_connections(self) -> BackendServer:
        # Snapshot in-flight counts and choose the least busy. Reading each
        # server's counter takes that server's lock (see BackendServer.inflight).
        return min(self.servers, key=lambda s: s.inflight)

    def pick(self, seed: int) -> BackendServer:
        if self.strategy == "round_robin":
            return self._pick_round_robin()
        if self.strategy == "random":
            return self._pick_random(seed)
        return self._pick_least_connections()

    def route(self, seed: int, work=None) -> tuple[str, bool]:
        """Route one request; returns (server_name, served?)."""
        srv = self.pick(seed)
        served = srv.handle(work)
        if not served:
            with self._unserved_lock:
                self.unserved += 1
        return srv.name, served

    # ── reporting ────────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        per_server = [
            {
                "name": s.name,
                "handled": s.handled,
                "rejected": s.rejected,
                "max_inflight": s.max_inflight,
                "capacity": s.capacity,
            }
            for s in self.servers
        ]
        handled = [s["handled"] for s in per_server]
        total_handled = sum(handled)
        spread = (max(handled) - min(handled)) if handled else 0
        return {
            "strategy": self.strategy,
            "servers": per_server,
            "total_handled": total_handled,
            "total_rejected": sum(s["rejected"] for s in per_server),
            "unserved": self.unserved,
            # Imbalance: 0 = perfectly even. Lower is better.
            "spread": spread,
        }
