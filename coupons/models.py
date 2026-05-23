from django.db import models
from django.db.models import F, Q


class Coupon(models.Model):
    code = models.CharField(max_length=50, unique=True)
    discount_percent = models.PositiveIntegerField()
    max_uses = models.PositiveIntegerField()
    usage_count = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(usage_count__lte=F("max_uses")),
                name="coupon_usage_within_max",
            ),
        ]

    def __str__(self):
        return self.code

    @property
    def is_available(self):
        return self.is_active and self.usage_count < self.max_uses
