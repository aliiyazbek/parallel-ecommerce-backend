"""Seed one high-stock product for the external HTTP checkout load test.

The external load tester (`scripts/load_test.py --endpoint checkout`) drives the
real authenticated checkout path over HTTP. That needs a product with enough
stock for every concurrent buyer. This command (re)creates it.

    python manage.py load_test_seed --stock 1000
"""

from django.core.management.base import BaseCommand

from products.models import Category, Product

SLUG = "loadtest-product"


class Command(BaseCommand):
    help = "Seed a high-stock product for the external checkout load test."

    def add_arguments(self, parser):
        parser.add_argument("--stock", type=int, default=1000)
        parser.add_argument("--price", default="10.00")

    def handle(self, *args, **opts):
        cat, _ = Category.objects.get_or_create(
            name="Load Test", defaults={"slug": "load-test"}
        )
        product, created = Product.objects.update_or_create(
            slug=SLUG,
            defaults={
                "category": cat,
                "name": "Load Test Product",
                "description": "Used by scripts/load_test.py --endpoint checkout.",
                "price": opts["price"],
                "stock": opts["stock"],
                "is_active": True,
            },
        )
        verb = "Created" if created else "Reset"
        self.stdout.write(
            f"{verb} product #{product.pk} '{product.name}' with stock={product.stock}."
        )
