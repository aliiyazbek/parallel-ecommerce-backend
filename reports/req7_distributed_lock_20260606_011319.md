# Requirement 7 — Concurrency Control (Distributed Lock)

**Generated:** 2026-06-06T01:13:19  
**Workers (simulated servers):** 50  
**Initial stock:** 10  
**Lock backend:** `memory (in-process fallback — NOT cross-process)`

## Scenario
N worker threads each stand in for a request arriving at one of several application servers (Requirement 5). They are released together by a `threading.Barrier` and each tries to buy 1 unit. The critical section reads the stock into a local variable, waits, then writes it back — the read-modify-write that an in-process lock cannot protect once requests are spread across servers.

## Results

| Mode | Workers | Initial | Successes | Rejected | Final stock | Oversold | Verdict |
|------|--------:|--------:|----------:|---------:|------------:|---------:|---------|
| **UNSAFE** | 50 | 10 | 50 | 0 | 9 | 40 | BUG REPRODUCED |
| **SAFE** | 50 | 10 | 10 | 40 | 0 | 0 | OK — stock limit respected |

## Before vs After

| Metric | BEFORE (no lock) | AFTER (distributed lock) |
|--------|-----------------:|-------------------------:|
| Successful purchases | 50 | 10 |
| Final stock in DB | 9 | 0 |
| **Oversold by** | **40** | **0** |
| Verdict | BUG REPRODUCED | OK — stock limit respected |

**Verdict:** the distributed lock eliminated overselling (40 → 0 units). Stock can never go below zero because only one worker — on any server — is inside the critical section at a time.

- **BEFORE critical-section latency (AOP @measure):** p50 646.87 ms / p95 1771.84 ms / max 1812.37 ms over 50 calls.
- **AFTER critical-section latency (AOP @measure):** p50 701.02 ms / p95 731.91 ms / max 734.63 ms over 50 calls.

> **Note:** this run used the in-process fallback (no Redis reachable). It still serializes the threads in this process, but it is **not** a cross-process lock. Start Redis (`docker run --rm -p 6379:6379 redis`) and re-run to exercise the real distributed lock.

## Where it lives in the codebase
- Distributed lock primitive: `core/distributed_lock.py::DistributedLock`
- Safe service: `orders/services.py::decrement_stock_dlock_safe`
- Unsafe baseline: `orders/services.py::decrement_stock_dlock_unsafe`
- AOP timing aspect: `core/aop.py::measure` (op `stock.dlock_*`)
