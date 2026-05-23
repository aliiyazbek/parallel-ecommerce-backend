from rest_framework import serializers
from .models import Coupon


class CouponSerializer(serializers.ModelSerializer):
    is_available = serializers.BooleanField(read_only=True)

    class Meta:
        model = Coupon
        fields = (
            "id", "code", "discount_percent",
            "max_uses", "usage_count", "is_active",
            "is_available", "created_at",
        )
        read_only_fields = ("usage_count", "created_at")


class RedeemSerializer(serializers.Serializer):
    code = serializers.CharField()
