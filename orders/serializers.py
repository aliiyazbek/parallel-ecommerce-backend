from rest_framework import serializers
from products.models import Product
from .models import Cart, CartItem, Order, OrderItem


class CartItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)
    price = serializers.DecimalField(
        source="product.price", read_only=True, max_digits=10, decimal_places=2
    )
    subtotal = serializers.DecimalField(
        read_only=True, max_digits=10, decimal_places=2
    )

    class Meta:
        model = CartItem
        fields = ("id", "product", "product_name", "price", "quantity", "subtotal")


class CartSerializer(serializers.ModelSerializer):
    items = CartItemSerializer(many=True, read_only=True)
    total = serializers.DecimalField(
        read_only=True, max_digits=10, decimal_places=2
    )

    class Meta:
        model = Cart
        fields = ("id", "items", "total", "created_at", "updated_at")


class AddToCartSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    quantity = serializers.IntegerField(min_value=1, default=1)


class OrderItemSerializer(serializers.ModelSerializer):
    subtotal = serializers.DecimalField(
        read_only=True, max_digits=10, decimal_places=2
    )

    class Meta:
        model = OrderItem
        fields = ("id", "product", "product_name", "price", "quantity", "subtotal")


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = (
            "id", "status", "shipping_address", "total",
            "items", "created_at", "updated_at",
        )
        read_only_fields = ("status", "total", "created_at", "updated_at")


class CheckoutSerializer(serializers.Serializer):
    shipping_address = serializers.CharField()
