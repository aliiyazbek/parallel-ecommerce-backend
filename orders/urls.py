from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    CartView, AddToCartView, CartItemDetailView,
    CheckoutView, OrderViewSet,
)

router = DefaultRouter()
router.register("orders", OrderViewSet, basename="order")

urlpatterns = [
    path("cart/", CartView.as_view(), name="cart"),
    path("cart/items/", AddToCartView.as_view(), name="cart-add"),
    path("cart/items/<int:pk>/", CartItemDetailView.as_view(), name="cart-item"),
    path("checkout/", CheckoutView.as_view(), name="checkout"),
    path("", include(router.urls)),
]
