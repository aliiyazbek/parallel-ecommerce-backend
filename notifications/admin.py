from django.contrib import admin
from .models import AsyncTask


@admin.register(AsyncTask)
class AsyncTaskAdmin(admin.ModelAdmin):
    list_display    = ("id", "task_type", "status", "retries", "created_at", "updated_at")
    list_filter     = ("status", "task_type")
    readonly_fields = ("created_at", "updated_at", "error_log", "payload")
    ordering        = ("-created_at",)
    search_fields   = ("task_type",)