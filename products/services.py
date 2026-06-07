"""Product read services (Requirement #6 — Distributed Caching).

Two implementations of "fetch a product by id", used by the demo to show the
before/after effect of putting a distributed cache in front of the database:

* `get_product_uncached`  — BEFORE: always queries the database.
* `get_product_cached`    — AFTER:  cache-aside via Redis (see `core/cache.py`).

`invalidate_product` is the write-side half of cache-aside: whenever a product
changes, its cache entry is dropped so the next read reloads fresh data — this
is what keeps the distributed cache consistent across all nodes.
"""

from __future__ import annotations

import json
import time

from core.aop import measure
from core import cache
from .models import Product

# Simulated database query cost. A real product read joins category, computes
# availability, etc.; we add a small fixed delay so the cache's value is
# *measurable* in the demo. The cached path skips this entirely on a hit.
DB_QUERY_SECONDS = 0.015


def _product_key(product_id: int) -> str:
    return f"product:{product_id}"


@measure("db.product_query")
def _load_product_from_db(product_id: int) -> dict | None:
    """The expensive source-of-truth read. Both paths ultimately call this on a
    cache MISS; the cached path avoids it on a HIT."""
    try:
        p = Product.objects.get(pk=product_id, is_active=True)
    except Product.DoesNotExist:
        return None
    # Simulate real per-query work (joins / serialization / availability calc).
    time.sleep(DB_QUERY_SECONDS)
    return {
        "id": p.pk,
        "name": p.name,
        "price": str(p.price),
        "stock": p.stock,
    }


@measure("product.read_uncached")
def get_product_uncached(product_id: int) -> dict | None:
    # BEFORE: every call pays the full database query cost — no caching at all.
    return _load_product_from_db(product_id)


@measure("product.read_cached")
def get_product_cached(product_id: int, ttl: float = 30.0) -> dict | None:
    # AFTER: cache-aside. Try the distributed cache first; only touch the DB on
    # a miss, then populate the cache so subsequent reads are served from Redis.
    key = _product_key(product_id)

    raw = cache.cache_get(key)
    if raw is not None:
        return json.loads(raw)          # HIT — no database query at all

    data = _load_product_from_db(product_id)   # MISS — pay for the DB once
    if data is not None:
        cache.cache_set(key, json.dumps(data), ttl=ttl)
    return data


def invalidate_product(product_id: int) -> None:
    # Write-side of cache-aside: drop the entry so the next read reloads. In a
    # distributed cache this invalidation is seen by EVERY node at once.
    cache.cache_delete(_product_key(product_id))
