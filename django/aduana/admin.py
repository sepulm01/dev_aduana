from django.contrib import admin

from aduana.models import ContainerDetection, ContainerEvent


@admin.register(ContainerEvent)
class ContainerEventAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "container_code",
        "seal_status",
        "seal_confidence",
        "timestamp_start",
        "timestamp_end",
    ]
    list_filter = ["seal_status", "timestamp_start"]
    search_fields = ["container_code"]


@admin.register(ContainerDetection)
class ContainerDetectionAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "class_label",
        "confidence",
        "source_id",
        "timestamp",
        "ocr_text",
        "ocr_confidence",
    ]
    list_filter = ["class_id", "source_id", "ocr_processed", "timestamp"]
    search_fields = ["ocr_text"]
    readonly_fields = ["crop"]
