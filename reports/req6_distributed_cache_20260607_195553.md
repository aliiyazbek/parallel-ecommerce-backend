# Requirement 6 — Distributed Caching (Redis, cache-aside)

**Generated:** 2026-06-07T19:55:53  
**Cache backend:** `memory (in-process fallback — NOT distributed)`  
**Workload:** 1000 reads of ONE popular product over 20 concurrent threads  
**Simulated DB query cost:** 15 ms each

> ⚠️ Redis was not reachable, so this run used the in-process fallback cache. The numbers still show the cache-aside effect, but a real deployment must run Redis for the cache to be shared across nodes. Start it with `docker run --rm -p 6379:6379 redis`.

## Scenario
Many users read the same hot product at once. **BEFORE** every read runs the database query. **AFTER** reads use cache-aside: the first read misses and loads from the DB, every later read is served from the distributed cache and skips the database entirely.

## Before vs After

| Metric | BEFORE (no cache) | AFTER (Redis cache) |
|--------|------------------:|--------------------:|
| Reads | 1000 | 1000 |
| **Database queries** | **1000** | **20** |
| Cache hits | 0 | 980 |
| Cache misses | 0 | 20 |
| Hit ratio | 0.0% | 98.0% |
| Read latency p50 (ms) | 28.88 | 0.01 |
| Read latency p95 (ms) | 48.19 | 0.01 |
| Wall time (ms) | 1550 | 57 |

**Verdict:** the cache eliminated **980** of **1000** database queries (**98.0%** fewer), reaching a **98.0%** hit ratio, and cut total wall time by **27.2x**. After the first miss, popular-product reads no longer touch the database at all — exactly the goal of Requirement 6.

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
