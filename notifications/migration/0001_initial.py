from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="AsyncTask",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("task_type",   models.CharField(max_length=50, choices=[("send_invoice", "Send Invoice Email"), ("send_notification", "Send Order Notification")])),
                ("payload",     models.JSONField()),
                ("status",      models.CharField(max_length=10, choices=[("pending", "Pending"), ("claimed", "Claimed"), ("done", "Done"), ("failed", "Failed")], default="pending", db_index=True)),
                ("retries",     models.PositiveSmallIntegerField(default=0)),
                ("max_retries", models.PositiveSmallIntegerField(default=3)),
                ("error_log",   models.TextField(blank=True)),
                ("created_at",  models.DateTimeField(auto_now_add=True)),
                ("updated_at",  models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Async Task",
                "verbose_name_plural": "Async Tasks",
                "ordering": ["created_at"],
            },
        ),
    ]