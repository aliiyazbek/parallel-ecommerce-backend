from rest_framework import viewsets, permissions, status
from rest_framework.views import APIView
from rest_framework.response import Response

from .models import Coupon
from .serializers import CouponSerializer, RedeemSerializer
from .services import redeem_safe, redeem_unsafe


class IsAdminOrReadOnly(permissions.BasePermission):
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return bool(request.user and request.user.is_staff)


class CouponViewSet(viewsets.ModelViewSet):
    queryset = Coupon.objects.all()
    serializer_class = CouponSerializer
    permission_classes = [IsAdminOrReadOnly]
    lookup_field = "code"


def _redeem_response(result):
    ok, reason = result
    if ok:
        return Response({"detail": "Redeemed"}, status=status.HTTP_200_OK)
    if reason == "invalid_code":
        return Response({"detail": "Invalid code"}, status=status.HTTP_404_NOT_FOUND)
    if reason == "inactive":
        return Response({"detail": "Coupon is inactive"}, status=status.HTTP_400_BAD_REQUEST)
    if reason == "exhausted":
        return Response({"detail": "Coupon is fully redeemed"}, status=status.HTTP_409_CONFLICT)
    return Response({"detail": reason}, status=status.HTTP_400_BAD_REQUEST)


class RedeemSafeView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "coupon_redeem"

    def post(self, request):
        serializer = RedeemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _redeem_response(redeem_safe(serializer.validated_data["code"]))


class RedeemUnsafeView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "coupon_redeem"

    def post(self, request):
        serializer = RedeemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _redeem_response(redeem_unsafe(serializer.validated_data["code"]))
