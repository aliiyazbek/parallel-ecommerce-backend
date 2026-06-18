"""Requirement #5 demo — REAL Load Distribution across multiple instances.

This is the version the brief asks for: load is distributed across several REAL
application instances — separate OS processes on different ports — NOT thread
pools inside one process. A load balancer forwards real HTTP requests and we
tally which instance answered each one.

First, in one terminal, start the instances:

    python scripts/start_instances.py            # ports 8001 8002 8003

Then, in another terminal, drive load through the balancer:

    python manage.py demo_load_distribution_real
    python manage.py demo_load_distribution_real --requests 600 --concurrency 30

BEFORE = every request goes to ONE instance (no balancing).
AFTER  = requests are balanced across ALL instances by the chosen strategy.
"""

from __future__ import annotations

import concurrent.futures
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from core.http_load_balancer import HttpLoadBalancer

REPORT_DIR = Path(settings.BASE_DIR) / "reports"
BANNER = "=" * 72
SUB = "-" * 72


def discover_instances(ports: list[int]) -> list[str]:
    """Return base URLs of instances that answer /api/whoami/."""
    alive = []
    for port in ports:
        url = f"http://127.0.0.1:{port}"
        try:
            r = requests.get(url + "/api/whoami/", timeout=1.5)
            if r.status_code == 200:
                alive.append(url)
        except requests.RequestException:
            pass
    return alive


def fire(do_request, total: int, concurrency: int, path: str) -> dict:
    """Send `total` requests with `concurrency` worker threads; tally results."""
    by_instance: Counter = Counter()
    latencies: list[float] = []
    errors = 0

    def one(_i):
        nonlocal errors
        t0 = time.perf_counter()
        res = do_request(path)
        dt = time.perf_counter() - t0
        if res.get("ok"):
            by_instance[res["instance"]] += 1
            latencies.append(dt)
        else:
            errors += 1
        return res

    wall0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        results = list(ex.map(one, range(total)))
    wall_ms = (time.perf_counter() - wall0) * 1000

    latencies.sort()
    p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] * 1000 if latencies else 0
    return {
        "total": total,
        "errors": errors,
        "wall_ms": round(wall_ms),
        "p95_ms": round(p95, 1),
        "by_instance": dict(by_instance),
        # Ordered list (task index → serving instance) for per-task routing output.
        "tasks": [r.get("instance") for r in results],
    }


def spread(by_instance: dict) -> int:
    if not by_instance:
        return 0
    return max(by_instance.values()) - min(by_instance.values())


def port_of(instance: str) -> str:
    """Extract the port from an instance label ('instance-8001') or a URL."""
    s = str(instance).rsplit(":", 1)[-1]   # 'http://127.0.0.1:8001' -> '8001'
    return s.rsplit("-", 1)[-1]            # 'instance-8001' -> '8001'


def write_report(before, after, strat_runs, chosen, instances, opts) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORT_DIR / f"req5_load_distribution_real_{ts}.md"
    after = strat_runs[chosen]

    L: list[str] = []
    L.append("# Requirement 5 — Load Distribution (REAL multiple instances)")
    L.append("")
    L.append(f"**Generated:** {datetime.now().isoformat(timespec='seconds')}  ")
    L.append(f"**Instances (separate processes / ports):** {len(instances)}  ")
    for u in instances:
        L.append(f"- {u}")
    L.append(f"**Workload:** {opts['requests']} HTTP requests, "
             f"concurrency {opts['concurrency']}, path `{opts['path']}`")
    L.append("")
    L.append("## What 'instance' means here")
    L.append(
        "Each instance is a **separate operating-system process** running the "
        "same Django app and listening on its **own port** (8001, 8002, ...), "
        "started by `scripts/start_instances.py`. This is the process/node model "
        "the brief requires — **not** thread pools inside one process. Every "
        "response carries the serving instance's name (`/api/whoami/`), so the "
        "distribution below is measured from real HTTP responses."
    )
    L.append("")
    L.append("## Before vs After")
    L.append("")
    L.append("| Setup | Requests | Errors | Wall (ms) | p95 (ms) | Spread (max−min per instance) |")
    L.append("|-------|---------:|-------:|----------:|---------:|------------------------------:|")
    L.append(f"| **BEFORE - one instance** | {before['total']} | {before['errors']} | "
             f"{before['wall_ms']} | {before['p95_ms']} | {spread(before['by_instance'])} |")
    L.append(f"| **AFTER - {len(instances)} instances ({chosen})** | {after['total']} | "
             f"{after['errors']} | {after['wall_ms']} | {after['p95_ms']} | "
             f"{spread(after['by_instance'])} |")
    L.append("")
    L.append("### Requests handled per instance")
    L.append("")
    L.append("**BEFORE (no balancing):**")
    L.append("")
    L.append("| Instance | Requests handled |")
    L.append("|----------|-----------------:|")
    for inst, n in sorted(before["by_instance"].items()):
        L.append(f"| {inst} | {n} |")
    L.append("")
    L.append(f"**AFTER (balanced, {chosen}):**")
    L.append("")
    L.append("| Instance | Requests handled |")
    L.append("|----------|-----------------:|")
    for inst, n in sorted(after["by_instance"].items()):
        L.append(f"| {inst} | {n} |")
    L.append("")
    L.append("### Per-task routing (sample — first 15 tasks)")
    L.append("")
    L.append("```")
    for i, inst in enumerate(after.get("tasks", [])[:15], start=1):
        L.append(f"Task {i} -> Handled by node on port {port_of(inst)}")
    L.append("```")
    L.append("")
    L.append("## Strategy comparison (same workload)")
    L.append("")
    L.append("| Strategy | Spread (max−min) | Wall (ms) | p95 (ms) | Errors |")
    L.append("|----------|-----------------:|----------:|---------:|-------:|")
    for s, r in strat_runs.items():
        mark = " ✅" if s == chosen else ""
        L.append(f"| `{s}`{mark} | {spread(r['by_instance'])} | {r['wall_ms']} | "
                 f"{r['p95_ms']} | {r['errors']} |")
    L.append("")
    L.append("### Strategy chosen & justification")
    L.append(
        f"`{chosen}` is adopted: with uniform request cost it splits traffic most "
        f"evenly across the instances (lowest spread) while needing no shared "
        f"state, which keeps routing lock-free. `least_connections` is preferable "
        f"when request costs vary widely — it routes to the least-busy instance — "
        f"but it must read a shared, mutex-guarded in-flight counter on every "
        f"routing decision."
    )
    L.append("")
    L.append("## Where it lives in the codebase")
    L.append("- Instance identity + endpoint: `core/instance_info.py`, `core/views.py` (`/api/whoami/`)")
    L.append("- Instance launcher: `scripts/start_instances.py`")
    L.append("- Load balancer: `core/http_load_balancer.py`")
    L.append("- This demo: `orders/management/commands/demo_load_distribution_real.py`")
    L.append("")

    path.write_text("\n".join(L), encoding="utf-8")
    return path


class Command(BaseCommand):
    help = "Req 5 demo: REAL load distribution across multiple instances on different ports."

    def add_arguments(self, parser):
        parser.add_argument("--ports", type=int, nargs="+", default=[8001, 8002, 8003])
        parser.add_argument("--requests", type=int, default=300)
        parser.add_argument("--concurrency", type=int, default=20)
        parser.add_argument("--path", default="/api/whoami/")
        parser.add_argument("--no-report", action="store_true")

    def handle(self, *args, **opts):
        ports = opts["ports"]
        path = opts["path"]

        print()
        print(BANNER)
        print(" Req 5 - REAL Load Distribution (multiple instances / ports)")
        print(BANNER)
        print(f" Probing instances on ports: {', '.join(map(str, ports))}")

        instances = discover_instances(ports)
        if not instances:
            print()
            print(" !! No instances are running.")
            print(" Start them first in another terminal:")
            print("    python scripts/start_instances.py "
                  f"--ports {' '.join(map(str, ports))}")
            return
        print(f" Found {len(instances)} live instance(s): {', '.join(instances)}")
        print(SUB)

        # BEFORE: all traffic to a single instance (no balancing).
        solo = HttpLoadBalancer([instances[0]], strategy="round_robin")
        before = fire(solo.request, opts["requests"], opts["concurrency"], path)
        print(f"  BEFORE - one instance       wall={before['wall_ms']:>6}ms  "
              f"p95={before['p95_ms']:>6}ms  errors={before['errors']}  "
              f"spread={spread(before['by_instance'])}")

        # AFTER: balance across ALL instances, trying each strategy.
        strat_runs = {}
        for strategy in HttpLoadBalancer.STRATEGIES:
            lb = HttpLoadBalancer(instances, strategy=strategy)
            r = fire(lb.request, opts["requests"], opts["concurrency"], path)
            strat_runs[strategy] = r
            print(f"  AFTER - {strategy:<18} wall={r['wall_ms']:>6}ms  "
                  f"p95={r['p95_ms']:>6}ms  errors={r['errors']}  "
                  f"spread={spread(r['by_instance'])}")

        # Choose the most even strategy (lowest spread, then lowest p95).
        chosen = min(strat_runs,
                     key=lambda s: (spread(strat_runs[s]["by_instance"]),
                                    strat_runs[s]["p95_ms"]))
        after = strat_runs[chosen]

        print()
        print(BANNER)
        print(" SIDE-BY-SIDE - Before vs After")
        print(BANNER)
        print(f"  {'Metric':<26}{'BEFORE (1 inst)':>16}{'AFTER ('+chosen+')':>22}")
        print(f"  {'-'*26}{'-'*16:>16}{'-'*22:>22}")
        print(f"  {'Instances used':<26}{1:>16}{len(instances):>22}")
        print(f"  {'Spread (max-min)':<26}{spread(before['by_instance']):>16}"
              f"{spread(after['by_instance']):>22}")
        print(f"  {'p95 latency (ms)':<26}{before['p95_ms']:>16}{after['p95_ms']:>22}")
        print(f"  {'Errors':<26}{before['errors']:>16}{after['errors']:>22}")
        print()
        print("  Per-instance request counts:")
        print(f"    BEFORE: {before['by_instance']}")
        print(f"    AFTER : {after['by_instance']}")
        print()
        print(f"  RESULT: traffic went from 1 instance handling everything to "
              f"{len(instances)} instances sharing the load (strategy: {chosen}).")

        # Per-task routing, in the exact format the brief illustrates.
        tasks = after.get("tasks", [])
        sample = min(12, len(tasks))
        print()
        print(f"  Per-task routing (strategy: {chosen}) — first {sample} of {len(tasks)}:")
        for i, inst in enumerate(tasks[:sample], start=1):
            print(f"    Task {i} -> Handled by node on port {port_of(inst)}")

        if not opts["no_report"]:
            p = write_report(before, after, strat_runs, chosen, instances, opts)
            print(f"\nReport saved to: {p}")
