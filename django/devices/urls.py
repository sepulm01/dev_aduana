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
]
