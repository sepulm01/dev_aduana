from django.contrib import admin
from django.utils.html import format_html

from detections.models import Detection, IdentityGroup, IdentityLog


class IdentityLogInline(admin.TabularInline):
    model = IdentityLog
    extra = 0
    readonly_fields = ["action", "details", "operator", "timestamp"]
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


class DetectionInline(admin.TabularInline):
    model = Detection
    extra = 0
    max_num = 50
    readonly_fields = [
        "id",
        "device",
        "object_id",
        "class_label",
        "quality_score",
        "timestamp",
        "has_crop",
        "bbox_display",
    ]
    fields = readonly_fields
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("device").order_by("-timestamp")[:50]

    @admin.display(boolean=True)
    def has_crop(self, obj):
        return bool(obj.crop)

    @admin.display(description="BBox")
    def bbox_display(self, obj):
        return f"L{obj.bbox_left:.2f} T{obj.bbox_top:.2f} W{obj.bbox_width:.2f} H{obj.bbox_height:.2f}"


@admin.register(IdentityGroup)
class IdentityGroupAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "detection_count",
        "first_seen",
        "last_seen",
        "created_at",
    ]
    ordering = ["-created_at"]
    inlines = [DetectionInline, IdentityLogInline]
    search_fields = ["id"]


@admin.register(IdentityLog)
class IdentityLogAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "identity_group_link",
        "action",
        "operator",
        "timestamp",
    ]
    list_filter = ["action"]
    ordering = ["-timestamp"]
    readonly_fields = [
        "identity_group",
        "action",
        "details",
        "operator",
        "timestamp",
    ]

    @admin.display(description="Identity")
    def identity_group_link(self, obj):
        url = f"/admin/detections/identitygroup/{obj.identity_group_id}/change/"
        return format_html(
            '<a href="{}">#{}</a>', url, obj.identity_group_id
        )


@admin.register(Detection)
class DetectionAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "object_id",
        "class_label",
        "device",
        "identity_group_link",
        "quality_score",
        "timestamp",
        "has_crop",
    ]
    list_filter = ["class_label", "device"]
    ordering = ["-timestamp"]
    readonly_fields = ["identity_summary"]

    @admin.display(boolean=True)
    def has_crop(self, obj):
        return bool(obj.crop)

    @admin.display(description="Identity")
    def identity_group_link(self, obj):
        if obj.identity_group_id:
            url = f"/admin/detections/identitygroup/{obj.identity_group_id}/change/"
            return format_html(
                '<a href="{}">#{}</a>', url, obj.identity_group_id
            )
        return "-"

    @admin.display(description="Identity Summary")
    def identity_summary(self, obj):
        if obj.identity_group:
            return f"#{obj.identity_group.pk} ({obj.identity_group.detection_count} visits)"
        return "Unmatched"
