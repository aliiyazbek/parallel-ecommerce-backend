# Requirement 6 — Distributed Caching (Redis, cache-aside)

**Generated:** 2026-06-18T02:21:53  
**Cache backend:** `redis`  
**Workload:** 1000 reads of ONE popular product over 20 concurrent threads  
**Simulated DB query cost:** 15 ms each

## Scenario
Many users read the same hot product at once. **BEFORE** every read runs the database query. **AFTER** reads use cache-aside: the first read misses and loads from the DB, every later read is served from the distributed cache and skips the database entirely.

## Before vs After

| Metric | BEFORE (no cache) | AFTER (Redis cache) |
|--------|------------------:|--------------------:|
| Reads | 1000 | 1000 |
| **Database queries** | **1000** | **1** |
| Cache hits | 0 | 999 |
| Cache misses | 0 | 1 |
| Hit ratio | 0.0% | 99.9% |
| Read latency p50 (ms) | 21.03 | 0.14 |
| Read latency p95 (ms) | 38.06 | 0.42 |
| Wall time (ms) | 1211 | 566 |

**Verdict:** the cache eliminated **999** of **1000** database queries (**99.9%** fewer), reaching a **99.9%** hit ratio, and cut total wall time by **2.1x**. After the first miss, popular-product reads no longer touch the database at all — exactly the goal of Requirement 6.

## How AOP produced these numbers
Every cache operation (`cache.get/set/delete`) and the DB query (`db.product_query`) are wrapped by the `@measure` aspect in `core/aop.py`. The aspect records one latency sample per call into a thread-safe collector; this command reads `perf.stats(...)` and the cache hit/miss counters afterwards. No timing code lives inside the cache or the product services.

## Why a *distributed* cache (not a per-process dict)
Under Load Distribution (Req 5) there are several application nodes. A local dict in node A is invisible to node B, so each node would keep a separate, possibly stale copy. A shared Redis cache gives every node ONE view and ONE place to invalidate on a write.

## Cache invalidation (consistency)
`products/services.py::invalidate_product` deletes the cached entry on any product change. It is wired into `products/views.py::ProductViewSet.perform_update / perform_destroy`, so an admin edit drops the stale entry and the next read reloads fresh data on every node.

## Where it lives in the codebase
- Cache layer: `core/cache.py` (Redis + in-process fallback)
- Cached vs uncached reads: `products/services.py`
- Invalidation on write: `products/views.py::ProductViewSet`
- This demo: `orders/management/commands/demo_distributed_cache.py`
