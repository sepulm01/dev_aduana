from django.contrib import admin

from detections.models import Detection


@admin.register(Detection)
class DetectionAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "object_id",
        "class_label",
        "quality_score",
        "timestamp",
        "has_crop",
    ]
    list_filter = ["class_label", "device"]
    ordering = ["-timestamp"]

    @admin.display(boolean=True)
    def has_crop(self, obj):
        return bool(obj.crop)
