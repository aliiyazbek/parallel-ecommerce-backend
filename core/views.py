"""Health / identity endpoints used by the Load Distribution demo (Req #5).

`/api/whoami/` returns which INSTANCE (process + port) handled the request. The
real load balancer fires many requests at the pool and tallies the responses by
instance, so the distribution across separate processes is directly observable —
this is the "multiple instances on different ports" model, not thread pools.

It optionally performs a real cached product read so the same request exercises
the distributed cache (Req #6) and could take the distributed lock (Req #7),
making the instances do representative work rather than a trivial ping.
"""

from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from core.instance_info import instance_info
from products.models import Product
from products.services import get_product_cached_with_hit


@api_view(["GET"])
@permission_classes([AllowAny])
def whoami(request):
    """Report the serving instance; optionally read ?product=<id> via the cache."""
    payload = instance_info()

    product_id = request.query_params.get("product")
    if product_id:
        try:
            data, was_hit = get_product_cached_with_hit(int(product_id))
            payload["product"] = data
            # True only when served straight from the shared cache (no DB query).
            payload["served_from_cache"] = was_hit
        except (ValueError, Product.DoesNotExist):
            payload["product"] = None

    return Response(payload)
