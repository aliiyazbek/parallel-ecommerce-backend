# Requirement 5 — Load Distribution

**Generated:** 2026-06-06T01:10:03  
**Burst size:** 200 simultaneous requests

## Scenario
Requests arrive at a steady rate. We compare ONE node (a single machine's worker pool) against several identical nodes behind a load balancer (horizontal scale-out). Each node is a worker pool (`threading.BoundedSemaphore`) that rejects requests it cannot admit. This is the realistic load-distribution question: one machine has a fixed worker limit, so the only way to serve more traffic is to add more machines and balance across them.

## Before vs After

| Setup | Servers | Total capacity | Handled | Rejected | Wall (ms) | p95 (ms) |
|-------|--------:|---------------:|--------:|---------:|----------:|---------:|
| **BEFORE - single node** | 1 | 6 | 91 | 109 | 844 | 51 |
| **AFTER - 4 nodes (round_robin)** | 4 | 24 | 200 | 0 | 896 | 51 |

**Verdict:** distributing the load cut rejected requests from **109** to **0** (109 fewer) at the same total capacity, and lowered p95 latency from 51 ms to 51 ms. Spreading work across servers removes the single-instance bottleneck.

## Strategy comparison (same workload)

| Strategy | Handled | Rejected | Spread (max−min) | p95 (ms) |
|----------|--------:|---------:|-----------------:|---------:|
| `round_robin` ✅ | 200 | 0 | 0 | 51 |
| `random` | 200 | 0 | 0 | 51 |
| `least_connections` | 200 | 0 | 6 | 51 |

### Strategy chosen & justification

`round_robin` is adopted: it rejected the fewest requests (0), kept the per-server load most even (spread=0, i.e. max-min requests across servers), and held a competitive p95 latency (51 ms). Round-robin is chosen as the default when costs are uniform because it needs no shared state; least-connections wins when request costs vary because it routes to the least busy server at the cost of reading a shared counter.

### Per-server distribution under the chosen strategy

| Server | Handled | Rejected | Max in-flight | Capacity |
|--------|--------:|---------:|--------------:|---------:|
| srv-1 | 50 | 0 | 4 | 6 |
| srv-2 | 50 | 0 | 4 | 6 |
| srv-3 | 50 | 0 | 4 | 6 |
| srv-4 | 50 | 0 | 4 | 6 |

## Where it lives in the codebase
- Load balancer + strategies: `core/load_balancer.py`
- This demo: `orders/management/commands/demo_load_distribution.py`
- Connection to Req 7: multiple servers are exactly why an in-process lock is insufficient and a **distributed** lock (`core/distributed_lock.py`) is required.
