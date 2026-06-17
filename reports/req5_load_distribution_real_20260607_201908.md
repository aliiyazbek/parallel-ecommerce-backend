# Requirement 5 — Load Distribution (REAL multiple instances)

**Generated:** 2026-06-07T20:19:08  
**Instances (separate processes / ports):** 3  
- http://127.0.0.1:8001
- http://127.0.0.1:8002
- http://127.0.0.1:8003
**Workload:** 300 HTTP requests, concurrency 20, path `/api/whoami/`

## What 'instance' means here
Each instance is a **separate operating-system process** running the same Django app and listening on its **own port** (8001, 8002, ...), started by `scripts/start_instances.py`. This is the process/node model the brief requires — **not** thread pools inside one process. Every response carries the serving instance's name (`/api/whoami/`), so the distribution below is measured from real HTTP responses.

## Before vs After

| Setup | Requests | Errors | Wall (ms) | p95 (ms) | Spread (max−min per instance) |
|-------|---------:|-------:|----------:|---------:|------------------------------:|
| **BEFORE - one instance** | 300 | 0 | 826 | 40.9 | 0 |
| **AFTER - 3 instances (round_robin)** | 300 | 0 | 457 | 39.1 | 0 |

### Requests handled per instance

**BEFORE (no balancing):**

| Instance | Requests handled |
|----------|-----------------:|
| instance-8001 | 300 |

**AFTER (balanced, round_robin):**

| Instance | Requests handled |
|----------|-----------------:|
| instance-8001 | 100 |
| instance-8002 | 100 |
| instance-8003 | 100 |

## Strategy comparison (same workload)

| Strategy | Spread (max−min) | Wall (ms) | p95 (ms) | Errors |
|----------|-----------------:|----------:|---------:|-------:|
| `round_robin` ✅ | 0 | 457 | 39.1 | 0 |
| `random` | 3 | 455 | 42.7 | 0 |
| `least_connections` | 11 | 435 | 40.8 | 0 |

### Strategy chosen & justification
`round_robin` is adopted: with uniform request cost it splits traffic most evenly across the instances (lowest spread) while needing no shared state, which keeps routing lock-free. `least_connections` is preferable when request costs vary widely — it routes to the least-busy instance — but it must read a shared, mutex-guarded in-flight counter on every routing decision.

## Where it lives in the codebase
- Instance identity + endpoint: `core/instance_info.py`, `core/views.py` (`/api/whoami/`)
- Instance launcher: `scripts/start_instances.py`
- Load balancer: `core/http_load_balancer.py`
- This demo: `orders/management/commands/demo_load_distribution_real.py`
