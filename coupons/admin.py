from django.contrib import admin
from .models import Coupon


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = ("code", "discount_percent", "usage_count", "max_uses", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code",)
