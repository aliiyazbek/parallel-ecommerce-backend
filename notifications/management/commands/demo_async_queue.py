import time
import logging

from django.core.management.base import BaseCommand
from django.db import transaction

from notifications.models import AsyncTask
from notifications.worker import process_one_batch
from notifications.tasks import send_invoice_email, send_order_notification

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Demo: shows the difference between sync and async task execution"

    def handle(self, *args, **options):
        self.stdout.write("\n" + "="*60)
        self.stdout.write("   Requirement 3 Demo: Asynchronous Queue")
        self.stdout.write("="*60 + "\n")

        AsyncTask.objects.all().delete()

        self.stdout.write(self.style.WARNING("\n[1] Synchronous execution (no queue):"))
        self.stdout.write("    Request waits for invoice + notification to finish...\n")

        start = time.perf_counter()
        send_invoice_email({"order_id": 1, "user_email": "user@test.com", "total": "250.00"})
        send_order_notification({"order_id": 1, "username": "testuser", "item_count": 3})
        sync_time = time.perf_counter() - start

        self.stdout.write(self.style.ERROR(f"\n    Request time (sync): {sync_time:.3f}s\n"))

        self.stdout.write(self.style.WARNING("\n[2] Async execution (with queue):"))
        self.stdout.write("    Request only enqueues tasks and returns immediately...\n")

        start = time.perf_counter()
        with transaction.atomic():
            AsyncTask.enqueue(
                task_type=AsyncTask.TASK_INVOICE,
                payload={"order_id": 42, "user_email": "customer@shop.com", "total": "350.00"},
            )
            AsyncTask.enqueue(
                task_type=AsyncTask.TASK_NOTIFY,
                payload={"order_id": 42, "username": "customer", "item_count": 5},
            )
        async_time = time.perf_counter() - start

        self.stdout.write(self.style.SUCCESS(
            f"    Request returned in: {async_time:.4f}s  "
            f"(saved {sync_time - async_time:.3f}s for the user)\n"
        ))
        self.stdout.write(f"    Tasks waiting in queue: {AsyncTask.objects.filter(status='pending').count()}\n")

        self.stdout.write(self.style.WARNING("\n[3] Worker processing in background:"))
        self.stdout.write("    (In production: run 'python manage.py run_worker' in a separate terminal)\n")

        start = time.perf_counter()
        processed = process_one_batch(batch_size=10)
        worker_time = time.perf_counter() - start

        self.stdout.write(self.style.SUCCESS(
            f"\n    Worker finished {processed} tasks in {worker_time:.3f}s "
            f"(zero impact on any user request)\n"
        ))

        done  = AsyncTask.objects.filter(status="done").count()
        total = AsyncTask.objects.count()

        self.stdout.write("\n" + "="*60)
        self.stdout.write(self.style.SUCCESS("   Results"))
        self.stdout.write("="*60)
        self.stdout.write(f"  Request time (sync)  : {sync_time:.3f}s")
        self.stdout.write(f"  Request time (queue) : {async_time:.4f}s")
        self.stdout.write(f"  Improvement          : {((sync_time - async_time) / sync_time * 100):.1f}% faster")
        self.stdout.write(f"  Tasks completed      : {done}/{total}")
        self.stdout.write("="*60 + "\n")