# Requirement 6 — Distributed Caching: Design, AOP, and Before/After Impact

**Project:** High-Performance E-Commerce Backend Engine (Parallel Programming, 2026)
**Scope:** Requirement **#6 — Distributed Caching (Redis)** — integrate a caching
layer (Redis) to store the most-requested products and reduce direct queries to
the database.

Reproduce everything here with one command (it prints a BEFORE vs AFTER table
and writes a Markdown report into `reports/`):

```bash
python manage.py demo_distributed_cache
```

---

## 1. The problem

Reads dominate an e-commerce catalogue: the same few popular products are fetched
thousands of times, while their data barely changes. With no cache, **every**
read runs the database query — joins, availability calculation, serialization.
Under load the database becomes the bottleneck doing near-identical work over and
over.

The brief's goal: put a **caching layer (e.g. Redis)** in front of the database
to hold the hot products and cut direct DB queries.

---

## 2. The solution — `core/cache.py` + `products/services.py`

We use **Redis** with the **cache-aside** (lazy-loading) pattern:

```
READ:
    value = cache.get(key)
    if value is None:              # MISS → pay for the DB query once...
        value = query_database()
        cache.set(key, value, ttl) # ...then remember it
    return value                   # HIT  → no DB query at all

WRITE (product changed):
    cache.delete(key)              # invalidate so the next read reloads
```

| Piece | File | Role |
|-------|------|------|
| `cache_get / cache_set / cache_delete` | `core/cache.py` | Redis cache ops, AOP-instrumented, with hit/miss counters. |
| `get_product_uncached` | `products/services.py` | **BEFORE** — always queries the DB. |
| `get_product_cached` | `products/services.py` | **AFTER** — cache-aside via Redis. |
| `invalidate_product` | `products/services.py` | Write-side: drop the entry on change. |
| `ProductViewSet.perform_update/destroy` | `products/views.py` | Wires invalidation into the live API. |

### Why a *distributed* cache (Redis), not a per-process dict
Under Load Distribution (Req 5) the system runs several application nodes. A
local dict in node A is invisible to node B, so each node would keep its own,
possibly **stale** copy, and an update on one node would not invalidate the
others. A shared **Redis** cache gives every node **one** consistent view and
**one** place to invalidate — that is what makes it *distributed*.

### Graceful fallback
If Redis is unreachable, the cache falls back to a process-local dict so the demo
still runs. It is clearly labelled `memory (in-process fallback — NOT
distributed)` in `active_backend()` and in every report, so the limitation is
never hidden. For a real distributed cache, start Redis first:

```bash
docker run --rm -p 6379:6379 redis      # or Memurai on Windows
```

---

## 3. The AOP layer (how the numbers are produced)

Following the same approach as Reqs 5/7/8, **no timing code lives inside the cache
or product services**. The `@measure` aspect from `core/aop.py` wraps each
operation and pushes one latency sample to a thread-safe `PerfCollector`:

```python
@measure("cache.get")
def cache_get(key): ...

@measure("db.product_query")
def _load_product_from_db(product_id): ...

@measure("product.read_cached")
def get_product_cached(product_id, ttl=30): ...
```

The demo then reads everything back from the aspect after the run —
`perf.stats("db.product_query")` for the DB-query count, `perf.stats(
"product.read_cached")` for read latency — plus the cache hit/miss counters from
`cache.cache_stats()`. This is the project's "AOP for performance monitoring"
requirement applied to caching.

---

## 4. Before vs After (measured)

1000 reads of one popular product over 20 concurrent threads; each DB query
simulates ~15 ms of real work:

| Metric | BEFORE (no cache) | AFTER (Redis cache) |
|--------|------------------:|--------------------:|
| Reads | 1000 | 1000 |
| **Database queries** | **1000** | **20** |
| Hit ratio | 0.0% | **98.0%** |
| Read latency p50 (ms) | ~28 | **~0.01** |
| Read latency p95 (ms) | ~44 | **~0.01** |
| Wall time (ms) | ~1491 | **~69** |

**Impact:** the cache removed **980 of 1000** database queries (**98% fewer**),
cut read latency from tens of milliseconds to effectively zero, and made the
whole workload **~21× faster**. After the first load, popular-product reads no
longer touch the database at all.

### Why 20 misses, not 1? (thundering herd)
All 20 threads start on a **cold** cache and miss at nearly the same instant —
before any of them has finished the DB load and called `set`. So each of the 20
loads the product once; from then on every read is a hit. This *thundering herd*
on a cold key is a well-known cache behaviour. It is harmless here (20 of 1000),
and in production it can be bounded further with a short lock on the miss path
(we already have `core/distributed_lock.py` for exactly that) — left out here to
keep the cache-aside demo focused.

---

## 5. Cache invalidation (consistency)

A cache is only safe if stale data is removed on writes. `invalidate_product`
deletes the entry, and it is wired into the live API:

```python
class ProductViewSet(viewsets.ModelViewSet):
    def perform_update(self, serializer):
        product = serializer.save()
        invalidate_product(product.pk)     # drop stale entry; next read reloads

    def perform_destroy(self, instance):
        pk = instance.pk
        super().perform_destroy(instance)
        invalidate_product(pk)
```

Because the entry lives in shared Redis, this single delete invalidates the value
for **every** node at once — no node keeps serving the old data.

---

## 6. Synchronization points (thread-safety)

| Concern | Primitive | Location |
|---------|-----------|----------|
| Hit/miss counter updates | `threading.Lock` | `core/cache.py::_stats_lock` |
| In-process fallback store | `threading.Lock` | `core/cache.py::_memory_guard` |
| Atomic set-if-absent / TTL (real backend) | Redis `SET ... PX` | `core/cache.py::cache_set` |
| Perf sample aggregation (AOP) | `threading.Lock` | `core/aop.py::PerfCollector` |

---

## 7. How to reproduce

```bash
# Optional — start real Redis so the cache is genuinely distributed:
docker run --rm -p 6379:6379 redis

python manage.py demo_distributed_cache                 # default 1000 reads / 20 threads
python manage.py demo_distributed_cache --reads 2000 --threads 50
```

The command prints the BEFORE vs AFTER table and saves a timestamped report under
`reports/req6_distributed_cache_*.md`.
