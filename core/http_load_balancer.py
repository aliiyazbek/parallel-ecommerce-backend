"""A real HTTP load balancer over multiple application instances (Req #5).

Unlike the earlier in-process simulation (thread pools inside one process), this
balancer forwards each request over HTTP to one of several REAL Django instances
running as separate processes on different ports (started by
`scripts/start_instances.py`). It picks the target instance by a routing
**strategy** and reports, per request, which instance actually answered.

Strategies:
* round_robin       — cycle through the instances in order; stateless, even split
                      when request costs are uniform. The default.
* random            — pick uniformly at random; stateless and lock-free.
* least_connections — send to the instance with the fewest in-flight requests;
                      adapts to uneven request cost at the price of a shared,
                      mutex-guarded counter.
"""

from __future__ import annotations

import itertools
import random
import threading

import requests


class Backend:
    """One real application instance, addressed by its base URL."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.in_flight = 0
        self.handled = 0
        self.errors = 0


class HttpLoadBalancer:
    STRATEGIES = ("round_robin", "random", "least_connections")

    def __init__(self, base_urls: list[str], strategy: str = "random",
                 timeout: float = 5.0):
        if strategy not in self.STRATEGIES:
            raise ValueError(f"unknown strategy: {strategy}")
        self.backends = [Backend(u) for u in base_urls]
        self.strategy = strategy
        self.timeout = timeout
        self._rr = itertools.cycle(range(len(self.backends)))
        # ── Synchronization point ───────────────────────────────────────────
        # One lock guards the round-robin cursor and the in-flight counters,
        # which many client threads mutate concurrently while firing requests.
        self._guard = threading.Lock()

    def _pick(self) -> Backend:
        if self.strategy == "round_robin":
            with self._guard:
                idx = next(self._rr)
            return self.backends[idx]
        if self.strategy == "random":
            return random.choice(self.backends)
        # least_connections: choose the backend with the fewest in-flight calls.
        with self._guard:
            return min(self.backends, key=lambda b: b.in_flight)

    def request(self, path: str) -> dict:
        """Route ONE request and return {ok, instance, status, backend_url}."""
        backend = self._pick()
        with self._guard:
            backend.in_flight += 1
        try:
            resp = requests.get(backend.base_url + path, timeout=self.timeout)
            ok = 200 <= resp.status_code < 300
            instance = None
            if ok:
                try:
                    instance = resp.json().get("instance")
                except ValueError:
                    instance = None
            with self._guard:
                backend.handled += 1
                if not ok:
                    backend.errors += 1
            return {
                "ok": ok,
                "instance": instance or backend.base_url,
                "status": resp.status_code,
                "backend_url": backend.base_url,
            }
        except requests.RequestException as exc:
            with self._guard:
                backend.errors += 1
            return {
                "ok": False,
                "instance": backend.base_url,
                "status": None,
                "backend_url": backend.base_url,
                "error": type(exc).__name__,
            }
        finally:
            with self._guard:
                backend.in_flight -= 1

    def snapshot(self) -> list[dict]:
        with self._guard:
            return [
                {"url": b.base_url, "handled": b.handled, "errors": b.errors}
                for b in self.backends
            ]
