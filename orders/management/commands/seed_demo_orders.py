import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from orders.models import Order, OrderItem
from products.models import Category, Product


PRODUCT_CATALOG = [
    ("Wireless Mouse",      "25.00"),
    ("Mechanical Keyboard", "120.00"),
    ("USB-C Cable",          "8.50"),
    ("27\" Monitor",        "310.00"),
    ("Webcam HD",            "65.00"),
    ("Office Chair",        "240.00"),
    ("Desk Lamp",            "35.00"),
    ("Headphones",          "180.00"),
]


class Command(BaseCommand):
    help = "Seed paid orders dated yesterday for the batch-processing demo."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=500,
                            help="How many orders to create (default 500).")

    def handle(self, *args, **opts):
        count = opts["count"]
        User = get_user_model()

        user, _ = User.objects.get_or_create(
            username="demo_buyer",
            defaults={"email": "demo_buyer@example.com"},
        )

        category, _ = Category.objects.get_or_create(name="Demo")
        products = []
        for name, price in PRODUCT_CATALOG:
            p, _ = Product.objects.get_or_create(
                name=name,
                defaults={"price": Decimal(price), "stock": 10_000, "category": category},
            )
            products.append(p)

        yesterday = (timezone.now() - timedelta(days=1)).replace(hour=12, minute=0)

        self.stdout.write(f"Creating {count} paid orders dated {yesterday.date()}...")

        created = 0
        with transaction.atomic():
            for _ in range(count):
                order = Order.objects.create(
                    user=user,
                    status="paid",
                    shipping_address="123 Demo Street",
                    total=Decimal("0.00"),
                )
                Order.objects.filter(pk=order.pk).update(created_at=yesterday)

                items_in_order = random.randint(1, 3)
                total = Decimal("0.00")
                for product in random.sample(products, items_in_order):
                    qty = random.randint(1, 4)
                    OrderItem.objects.create(
                        order=order,
                        product=product,
                        product_name=product.name,
                        price=product.price,
                        quantity=qty,
                    )
                    total += product.price * qty
                Order.objects.filter(pk=order.pk).update(total=total)
                created += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done. {created} paid orders dated {yesterday.date()} are ready."
        ))
