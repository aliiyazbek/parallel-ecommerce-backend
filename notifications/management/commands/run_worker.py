from django.core.management.base import BaseCommand
from notifications.worker import run_forever


class Command(BaseCommand):
    help = "Run the async queue worker to process invoices and notifications"

    def add_arguments(self, parser):
        parser.add_argument("--poll",  type=float, default=2.0, help="Seconds to wait when queue is empty (default: 2)")
        parser.add_argument("--batch", type=int,   default=10,  help="Tasks to process per cycle (default: 10)")

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS(
            f"[Worker] Starting | poll={options['poll']}s | batch={options['batch']}"
        ))
        run_forever(poll_interval=options["poll"], batch_size=options["batch"])