from django.db import models


class AsyncTask(models.Model):

    TASK_INVOICE = "send_invoice"
    TASK_NOTIFY  = "send_notification"

    TASK_CHOICES = [
        (TASK_INVOICE, "Send Invoice Email"),
        (TASK_NOTIFY,  "Send Order Notification"),
    ]

    STATUS_PENDING = "pending"
    STATUS_CLAIMED = "claimed"
    STATUS_DONE    = "done"
    STATUS_FAILED  = "failed"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_CLAIMED, "Claimed"),
        (STATUS_DONE,    "Done"),
        (STATUS_FAILED,  "Failed"),
    ]

    task_type   = models.CharField(max_length=50, choices=TASK_CHOICES)
    payload     = models.JSONField()
    status      = models.CharField(
                      max_length=10,
                      choices=STATUS_CHOICES,
                      default=STATUS_PENDING,
                      db_index=True,
                  )
    retries     = models.PositiveSmallIntegerField(default=0)
    max_retries = models.PositiveSmallIntegerField(default=3)
    error_log   = models.TextField(blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Async Task"
        verbose_name_plural = "Async Tasks"

    def __str__(self):
        return f"AsyncTask({self.task_type}, order={self.payload.get('order_id')}, {self.status})"


    @classmethod
    def enqueue(cls, task_type: str, payload: dict) -> "AsyncTask":
        return cls.objects.create(task_type=task_type, payload=payload)

    def mark_claimed(self):
        self.status = self.STATUS_CLAIMED
        self.save(update_fields=["status", "updated_at"])

    def mark_done(self):
        self.status = self.STATUS_DONE
        self.save(update_fields=["status", "updated_at"])

    def mark_failed(self, error: str):
        self.retries += 1
        self.error_log = error
        if self.retries >= self.max_retries:
            self.status = self.STATUS_FAILED
        else:
            self.status = self.STATUS_PENDING
        self.save(update_fields=["status", "retries", "error_log", "updated_at"])