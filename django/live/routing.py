from django.urls import re_path
from live import consumers

websocket_urlpatterns = [
    re_path(
        r"ws/device/(?P<device_id>\d+)/$",
        consumers.DeviceConsumer.as_asgi(),
    ),
    re_path(
        r"ws/incidents/$",
        consumers.IncidentConsumer.as_asgi(),
    ),
]
