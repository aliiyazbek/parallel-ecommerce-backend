from django.db import transaction
from rest_framework import generics, status, permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from core.aop import log_execution, audit_action
from core.concurrency import limit_concurrency, drf_capacity_exceeded
from notifications.models import AsyncTask
from products.models import Product
from .models import Cart, CartItem, Order, OrderItem
from .serializers import (
    CartSerializer, AddToCartSerializer,
    OrderSerializer, CheckoutSerializer,
)


class CartView(generics.RetrieveAPIView):
    serializer_class = CartSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        cart, _ = Cart.objects.get_or_create(user=self.request.user)
        return cart


class AddToCartView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = AddToCartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product = serializer.validated_data["product"]
        quantity = serializer.validated_data["quantity"]

        if product.stock < quantity:
            return Response(
                {"detail": "Not enough stock."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cart, _ = Cart.objects.get_or_create(user=request.user)
        item, created = CartItem.objects.get_or_create(
            cart=cart, product=product, defaults={"quantity": quantity}
        )
        if not created:
            item.quantity += quantity
            item.save()

        return Response(CartSerializer(cart).data, status=status.HTTP_200_OK)


class CartItemDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, pk):
        try:
            item = CartItem.objects.get(pk=pk, cart__user=request.user)
        except CartItem.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        quantity = request.data.get("quantity")
        if quantity is None or int(quantity) < 1:
            return Response(
                {"detail": "quantity must be >= 1"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        item.quantity = int(quantity)
        item.save()
        return Response(CartSerializer(item.cart).data)

    def delete(self, request, pk):
        try:
            item = CartItem.objects.get(pk=pk, cart__user=request.user)
        except CartItem.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        cart = item.cart
        item.delete()
        return Response(CartSerializer(cart).data)


class CheckoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "checkout"

    # Decorator stack (outermost → innermost) layers the concurrency guarantees:
    #   limit_concurrency  → capacity cap / bulkhead: at most 3 checkouts run at
    #                        once, the rest get HTTP 503 (Requirement #2).
    #   transaction.atomic → payment + stock decrement + order rows are one
    #                        all-or-nothing unit, even under concurrent access
    #                        (Requirement #8 — ACID).
    @limit_concurrency(max_concurrent=3, timeout=2.0, on_reject=drf_capacity_exceeded)
    @log_execution()
    @audit_action(
        action="order.created",
        extract=lambda result, self, request: {
            "order_id": result.data.get("id"),
            "user":     request.user.username,
            "total":    str(result.data.get("total", "")),
        },
    )
    @transaction.atomic
    def post(self, request):
        serializer = CheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            cart = Cart.objects.get(user=request.user)
        except Cart.DoesNotExist:
            return Response(
                {"detail": "Cart is empty."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        items = list(cart.items.select_related("product"))
        if not items:
            return Response(
                {"detail": "Cart is empty."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── Synchronization point: pessimistic lock on every product in the cart ──
        # Product IDs are sorted so all concurrent checkouts acquire their row
        # locks in the SAME order — a consistent global lock ordering that
        # prevents deadlock. The locks serialize stock updates across orders so
        # two buyers can never oversell the same item (Requirement #1).
        product_ids = sorted({item.product_id for item in items})
        locked_products = {
            p.pk: p
            for p in Product.objects.select_for_update().filter(pk__in=product_ids)
        }

        total = 0
        for item in items:
            product = locked_products[item.product_id]
            if product.stock < item.quantity:
                return Response(
                    {"detail": f"Not enough stock for {product.name}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            total += product.price * item.quantity

        order = Order.objects.create(
            user=request.user,
            shipping_address=serializer.validated_data["shipping_address"],
            total=total,
        )
        for item in items:
            product = locked_products[item.product_id]
            OrderItem.objects.create(
                order=order,
                product=product,
                product_name=product.name,
                price=product.price,
                quantity=item.quantity,
            )
            product.stock -= item.quantity
            product.save(update_fields=["stock"])

        cart.items.all().delete()

        # Offload slow side-effects (invoice email, notification) onto the async
        # queue so they run OUTSIDE the request path — the user is not blocked
        # waiting for them, keeping checkout latency low (Requirement #3).
        AsyncTask.enqueue(
            task_type=AsyncTask.TASK_INVOICE,
            payload={
                "order_id":   order.pk,
                "user_email": request.user.email,
                "total":      str(order.total),
            },
        )
        AsyncTask.enqueue(
            task_type=AsyncTask.TASK_NOTIFY,
            payload={
                "order_id":   order.pk,
                "username":   request.user.username,
                "item_count": len(items),
            },
        )

        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)


class OrderViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = OrderSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Order.objects.filter(user=self.request.user).prefetch_related("items")

    @action(detail=True, methods=["post"])
    @audit_action(
        action="order.cancelled",
        extract=lambda result, self, request, pk=None: {
            "order_id": pk,
            "user":     request.user.username,
        },
    )
    def cancel(self, request, pk=None):
        order = self.get_object()
        if order.status not in ("pending", "paid"):
            return Response(
                {"detail": "Order cannot be cancelled."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        order.status = "cancelled"
        order.save()
        return Response(OrderSerializer(order).data)
