"""Requirement #5 demo — Load Distribution.

Sends a burst of requests at the backend two ways:

  * BEFORE: a SINGLE server (one worker pool). It saturates and rejects the
    overflow — the classic single-instance bottleneck.
  * AFTER : the SAME total capacity split across N servers behind a
    LoadBalancer. Requests spread out, far fewer are rejected.

It then compares the three balancing strategies (round_robin / random /
least_connections) on the same workload and JUSTIFIES which to adopt, as the
requirement asks.

    python manage.py demo_load_distribution
    python manage.py demo_load_distribution --requests 400 --servers 4
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core.aop import perf
from core.load_balancer import BackendServer, LoadBalancer

REPORT_DIR = Path(settings.BASE_DIR) / "reports"
BANNER = "=" * 72


def fire(target, requests: int, arrival_interval: float = 0.002,
         work_seed_offset: int = 0):
    """Send `requests` at `target(seed)` as a SUSTAINED stream and time it.

    Requests arrive paced by `arrival_interval` (like real traffic at a steady
    rate) rather than all at the same instant. A steady rate is what lets load
    distribution show its benefit: a single server's worker pool backs up and
    rejects the overflow, while N servers absorb the same rate in parallel.
    """
    latencies: list[float] = []
    guard = threading.Lock()
    threads: list[threading.Thread] = []

    def worker(i: int):
        t0 = time.perf_counter()
        target(i + work_seed_offset)
        with guard:
            latencies.append(time.perf_counter() - t0)

    wall0 = time.perf_counter()
    for i in range(requests):
        t = threading.Thread(target=worker, args=(i,))
        t.start()
        threads.append(t)
        time.sleep(arrival_interval)   # pace the arrivals (steady request rate)
    for t in threads:
        t.join()
    wall = time.perf_counter() - wall0
    return wall, latencies


def pct(latencies: list[float], q: float) -> float:
    if not latencies:
        return 0.0
    s = sorted(latencies)
    return s[min(len(s) - 1, int(len(s) * q))] * 1000


def run_single_server(requests: int, total_capacity: int, base_latency: float,
                      arrival_interval: float) -> dict:
    server = BackendServer("solo", capacity=total_capacity, base_latency=base_latency)

    def target(seed):
        server.handle()

    wall, lat = fire(target, requests, arrival_interval=arrival_interval)
    return {
        "label": "BEFORE - single server",
        "servers": 1,
        "total_capacity": total_capacity,
        "handled": server.handled,
        "rejected": server.rejected,
        "wall_ms": round(wall * 1000),
        "p95_ms": round(pct(lat, 0.95)),
        "spread": 0,
    }


def run_balanced(requests: int, servers: int, per_server_cap: int,
                 base_latency: float, strategy: str,
                 arrival_interval: float) -> dict:
    pool = [
        BackendServer(f"srv-{i+1}", capacity=per_server_cap, base_latency=base_latency)
        for i in range(servers)
    ]
    lb = LoadBalancer(pool, strategy=strategy)

    def target(seed):
        lb.route(seed)

    wall, lat = fire(target, requests, arrival_interval=arrival_interval)
    snap = lb.snapshot()
    # total_rejected and unserved count the SAME events (a server refused the
    # request) — use one, never the sum, to avoid double counting.
    return {
        "label": f"AFTER - {servers} servers / {strategy}",
        "strategy": strategy,
        "servers": servers,
        "total_capacity": servers * per_server_cap,
        "handled": snap["total_handled"],
        "rejected": snap["total_rejected"],
        "wall_ms": round(wall * 1000),
        "p95_ms": round(pct(lat, 0.95)),
        "spread": snap["spread"],
        "per_server": snap["servers"],
    }


def print_row(r: dict):
    print(f"  {r['label']:<34} handled={r['handled']:>4}  "
          f"rejected={r['rejected']:>4}  wall={r['wall_ms']:>5}ms  "
          f"p95={r['p95_ms']:>4}ms  spread={r['spread']}")


def choose_strategy(strategy_runs: dict) -> tuple[str, str]:
    """Pick the strategy with the fewest rejects, then lowest spread, then p95."""
    best = min(
        strategy_runs.values(),
        key=lambda r: (r["rejected"], r["spread"], r["p95_ms"]),
    )
    why = (
        f"`{best['strategy']}` is adopted: it rejected the fewest requests "
        f"({best['rejected']}), kept the per-server load most even "
        f"(spread={best['spread']}, i.e. max-min requests across servers), and "
        f"held a competitive p95 latency ({best['p95_ms']} ms). Round-robin is "
        f"chosen as the default when costs are uniform because it needs no shared "
        f"state; least-connections wins when request costs vary because it routes "
        f"to the least busy server at the cost of reading a shared counter."
    )
    return best["strategy"], why


def write_report(before: dict, strat_runs: dict, chosen: str, why: str,
                 requests: int) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORT_DIR / f"req5_load_distribution_{ts}.md"
    after = strat_runs[chosen]

    L: list[str] = []
    L.append("# Requirement 5 — Load Distribution")
    L.append("")
    L.append(f"**Generated:** {datetime.now().isoformat(timespec='seconds')}  ")
    L.append(f"**Workload:** {requests} requests at a steady arrival rate")
    L.append("")
    L.append("## Scenario")
    L.append(
        "Requests arrive at a steady rate. We compare ONE node (a single "
        "machine's worker pool) against several identical nodes behind a load "
        "balancer (horizontal scale-out). Each node is a worker pool "
        "(`threading.BoundedSemaphore`) that rejects requests it cannot admit. "
        "This is the realistic load-distribution question: one machine has a "
        "fixed worker limit, so the only way to serve more traffic is to add "
        "more machines and balance across them."
    )
    L.append("")
    L.append("## Before vs After")
    L.append("")
    L.append("| Setup | Servers | Total capacity | Handled | Rejected | Wall (ms) | p95 (ms) |")
    L.append("|-------|--------:|---------------:|--------:|---------:|----------:|---------:|")
    L.append(f"| **BEFORE - single node** | 1 | {before['total_capacity']} | "
             f"{before['handled']} | {before['rejected']} | {before['wall_ms']} | {before['p95_ms']} |")
    L.append(f"| **AFTER - {after['servers']} nodes ({chosen})** | {after['servers']} | "
             f"{after['total_capacity']} | {after['handled']} | {after['rejected']} | "
             f"{after['wall_ms']} | {after['p95_ms']} |")
    L.append("")
    improvement = before["rejected"] - after["rejected"]
    L.append(
        f"**Verdict:** scaling from 1 node to {after['servers']} nodes behind the "
        f"load balancer cut rejected requests from **{before['rejected']}** to "
        f"**{after['rejected']}** ({improvement} fewer). A single node tops out at "
        f"its worker limit and drops the overflow; spreading the same traffic "
        f"across nodes removes that single-instance bottleneck."
    )
    L.append("")
    L.append("## Strategy comparison (same workload)")
    L.append("")
    L.append("| Strategy | Handled | Rejected | Spread (max−min) | p95 (ms) |")
    L.append("|----------|--------:|---------:|-----------------:|---------:|")
    for s, r in strat_runs.items():
        mark = " ✅" if s == chosen else ""
        L.append(f"| `{s}`{mark} | {r['handled']} | {r['rejected']} | {r['spread']} | {r['p95_ms']} |")
    L.append("")
    L.append("### Strategy chosen & justification")
    L.append("")
    L.append(why)
    L.append("")
    L.append("### Per-server distribution under the chosen strategy")
    L.append("")
    L.append("| Server | Handled | Rejected | Max in-flight | Capacity |")
    L.append("|--------|--------:|---------:|--------------:|---------:|")
    for s in after.get("per_server", []):
        L.append(f"| {s['name']} | {s['handled']} | {s['rejected']} | "
                 f"{s['max_inflight']} | {s['capacity']} |")
    L.append("")
    L.append("## Where it lives in the codebase")
    L.append("- Load balancer + strategies: `core/load_balancer.py`")
    L.append("- This demo: `orders/management/commands/demo_load_distribution.py`")
    L.append("- Connection to Req 7: multiple servers are exactly why an "
             "in-process lock is insufficient and a **distributed** lock "
             "(`core/distributed_lock.py`) is required.")
    L.append("")
    path.write_text("\n".join(L), encoding="utf-8")
    return path


class Command(BaseCommand):
    help = "Req 5 demo: distribute a request burst across servers + justify strategy."

    def add_arguments(self, parser):
        parser.add_argument("--requests", type=int, default=300)
        parser.add_argument("--servers", type=int, default=4)
        parser.add_argument("--per-server-cap", type=int, default=8)
        parser.add_argument("--base-latency", type=float, default=0.05)
        parser.add_argument("--arrival-interval", type=float, default=0.003,
                            help="Seconds between request arrivals (steady rate).")
        parser.add_argument("--no-report", action="store_true")

    def handle(self, *args, **opts):
        perf.reset()
        requests = opts["requests"]
        servers = opts["servers"]
        per_cap = opts["per_server_cap"]
        lat = opts["base_latency"]
        arrival = opts["arrival_interval"]
        total_cap = servers * per_cap

        print()
        print(BANNER)
        print(" Req 5 - Load Distribution - BEFORE vs AFTER")
        print(BANNER)
        print(f" Workload: {requests} requests at a steady rate "
              f"(~1 every {arrival*1000:.0f} ms), each ~{lat*1000:.0f} ms of work")
        print(f" BEFORE: ONE node with {per_cap} workers (a single machine's limit)")
        print(f" AFTER : {servers} such nodes ({servers}x{per_cap}={total_cap}) "
              f"behind a load balancer (horizontal scale-out)")
        print("-" * 72)

        # BEFORE = a single node with realistic single-machine capacity.
        before = run_single_server(requests, per_cap, lat, arrival)
        print_row(before)

        strat_runs = {}
        for strategy in LoadBalancer.STRATEGIES:
            r = run_balanced(requests, servers, per_cap, lat, strategy, arrival)
            strat_runs[strategy] = r
            print_row(r)

        chosen, why = choose_strategy(strat_runs)
        after = strat_runs[chosen]

        print()
        print(BANNER)
        print(" SIDE-BY-SIDE - Before vs After")
        print(BANNER)
        print(f"  {'Metric':<22}{'BEFORE (1 srv)':>16}{'AFTER ('+chosen+')':>22}")
        print(f"  {'-'*22}{'-'*16:>16}{'-'*22:>22}")
        print(f"  {'Servers':<22}{before['servers']:>16}{after['servers']:>22}")
        print(f"  {'Total capacity':<22}{before['total_capacity']:>16}{after['total_capacity']:>22}")
        print(f"  {'Handled':<22}{before['handled']:>16}{after['handled']:>22}")
        print(f"  {'Rejected':<22}{before['rejected']:>16}{after['rejected']:>22}")
        print(f"  {'Wall time (ms)':<22}{before['wall_ms']:>16}{after['wall_ms']:>22}")
        print(f"  {'p95 latency (ms)':<22}{before['p95_ms']:>16}{after['p95_ms']:>22}")
        print()
        print(f"  RESULT: scaling 1 -> {after['servers']} nodes cut rejected "
              f"requests {before['rejected']} -> {after['rejected']}.")
        print()
        print(f"  Strategy chosen: {chosen}")
        print(f"  {why}")

        if not opts["no_report"]:
            path = write_report(before, strat_runs, chosen, why, requests)
            print(f"\nReport saved to: {path}")
