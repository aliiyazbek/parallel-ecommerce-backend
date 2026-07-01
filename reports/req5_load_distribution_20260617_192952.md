# Requirement 5 — Load Distribution

**Generated:** 2026-06-17T19:29:52  
**Workload:** 300 requests at a steady arrival rate

## Scenario
Requests arrive at a steady rate. We compare ONE node (a single machine's worker pool) against several identical nodes behind a load balancer (horizontal scale-out). Each node is a worker pool (`threading.BoundedSemaphore`) that rejects requests it cannot admit. This is the realistic load-distribution question: one machine has a fixed worker limit, so the only way to serve more traffic is to add more machines and balance across them.

## Before vs After

| Setup | Servers | Total capacity | Handled | Rejected | Wall (ms) | p95 (ms) |
|-------|--------:|---------------:|--------:|---------:|----------:|---------:|
| **BEFORE - single node** | 1 | 8 | 177 | 123 | 1189 | 51 |
| **AFTER - 4 nodes (round_robin)** | 4 | 32 | 300 | 0 | 1223 | 51 |

**Verdict:** scaling from 1 node to 4 nodes behind the load balancer cut rejected requests from **123** to **0** (123 fewer). A single node tops out at its worker limit and drops the overflow; spreading the same traffic across nodes removes that single-instance bottleneck.

## Strategy comparison (same workload)

| Strategy | Handled | Rejected | Spread (max−min) | p95 (ms) |
|----------|--------:|---------:|-----------------:|---------:|
| `round_robin` ✅ | 300 | 0 | 0 | 51 |
| `random` | 300 | 0 | 0 | 51 |
| `least_connections` | 300 | 0 | 19 | 51 |

### Strategy chosen & justification

`round_robin` is adopted: it rejected the fewest requests (0), kept the per-server load most even (spread=0, i.e. max-min requests across servers), and held a competitive p95 latency (51 ms). Round-robin is chosen as the default when costs are uniform because it needs no shared state; least-connections wins when request costs vary because it routes to the least busy server at the cost of reading a shared counter.

### Per-server distribution under the chosen strategy

| Server | Handled | Rejected | Max in-flight | Capacity |
|--------|--------:|---------:|--------------:|---------:|
| srv-1 | 75 | 0 | 4 | 8 |
| srv-2 | 75 | 0 | 4 | 8 |
| srv-3 | 75 | 0 | 4 | 8 |
| srv-4 | 75 | 0 | 4 | 8 |

## Where it lives in the codebase
- Load balancer + strategies: `core/load_balancer.py`
- This demo: `orders/management/commands/demo_load_distribution.py`
- Connection to Req 7: multiple servers are exactly why an in-process lock is insufficient and a **distributed** lock (`core/distributed_lock.py`) is required.
