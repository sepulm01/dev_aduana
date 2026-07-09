from django.urls import path

from aduana import views

urlpatterns = [
    path("", views.dashboard, name="aduana_dashboard"),
    path("<int:event_id>/", views.event_detail, name="aduana_event_detail"),
    path(
        "devices/<int:device_id>/analytics/",
        views.analytics_editor,
        name="analytics_editor",
    ),
    path(
        "api/devices/<int:device_id>/analytics/presets/",
        views.analytics_presets,
        name="analytics_presets",
    ),
    path(
        "api/devices/<int:device_id>/analytics/<str:preset_token>/shapes/",
        views.analytics_shapes,
        name="analytics_shapes",
    ),
    path(
        "api/devices/<int:device_id>/analytics/capture-snapshot/",
        views.analytics_capture_snapshot,
        name="analytics_capture_snapshot",
    ),
    path(
        "api/devices/<int:device_id>/analytics/disable/",
        views.analytics_disable,
        name="analytics_disable",
    ),
    path(
        "api/devices/<int:device_id>/analytics/apply/",
        views.analytics_apply,
        name="analytics_apply",
    ),
]
