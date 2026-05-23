from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import CouponViewSet, RedeemSafeView, RedeemUnsafeView

router = DefaultRouter()
router.register("coupons", CouponViewSet, basename="coupon")

urlpatterns = [
    path("coupons/redeem/", RedeemSafeView.as_view(), name="coupon-redeem"),
    path("coupons/redeem-unsafe/", RedeemUnsafeView.as_view(), name="coupon-redeem-unsafe"),
    path("", include(router.urls)),
]
