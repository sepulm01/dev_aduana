from django.urls import path
from devices import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("discover/", views.discover, name="discover"),
    path("api/discover/", views.discover, name="api_discover"),
    path("api/probe/", views.probe, name="api_probe"),
    path("api/devices/", views.add_device, name="add_device"),
    path("devices/<int:device_id>/", views.device_detail, name="device_detail"),
    path("devices/<int:device_id>/delete/", views.delete_device, name="delete_device"),
    path(
        "api/devices/<int:device_id>/profiles/",
        views.device_profiles,
        name="device_profiles",
    ),
    path(
        "api/devices/<int:device_id>/scan/",
        views.scan_device,
        name="scan_device",
    ),
    path(
        "api/devices/<int:device_id>/sync-time/",
        views.sync_time,
        name="sync_time_device",
    ),
    path("api/devices/sync-time/", views.sync_time, name="sync_time_all"),
    path(
        "api/devices/<int:device_id>/default-profile/",
        views.set_default_profile,
        name="set_default_profile",
    ),
    path(
        "api/devices/<int:device_id>/motion-config/",
        views.device_motion_config,
        name="device_motion_config",
    ),
    path(
        "api/devices/<int:device_id>/ivs/",
        views.device_ivs_config,
        name="device_ivs_config",
    ),
    path(
        "api/devices/<int:device_id>/events/",
        views.device_events,
        name="device_events",
    ),
    path(
        "api/devices/<int:device_id>/event-listener/",
        views.device_event_listener_toggle,
        name="device_event_listener_toggle",
    ),
    path(
        "devices/<int:device_id>/analytics/",
        views.analytics_editor,
        name="analytics_editor",
    ),
    path(
        "api/devices/<int:device_id>/analytics/snapshot/",
        views.analytics_snapshot,
        name="analytics_snapshot",
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
        "api/devices/<int:device_id>/analytics/apply/",
        views.analytics_apply,
        name="analytics_apply",
    ),
    path(
        "api/devices/<int:device_id>/analytics/goto-apply/",
        views.analytics_goto_and_apply,
        name="analytics_goto_and_apply",
    ),
    path(
        "api/devices/<int:device_id>/analytics/disable/",
        views.analytics_disable,
        name="analytics_disable",
    ),
    path(
        "api/devices/<int:device_id>/deepstream/preview/start/",
        views.deepstream_preview_start,
        name="deepstream_preview_start",
    ),
    path(
        "api/devices/<int:device_id>/deepstream/preview/stop/",
        views.deepstream_preview_stop,
        name="deepstream_preview_stop",
    ),
    path(
        "api/devices/<int:device_id>/deepstream/preview/keepalive/",
        views.deepstream_preview_keepalive,
        name="deepstream_preview_keepalive",
    ),
]
