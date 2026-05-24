import json
import threading
import time

from django.apps import AppConfig


class DevicesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "devices"

    def ready(self):
        import devices.signals  # noqa: F401

        from devices.models import Device
        from onvif_utils.client import OnvifClient
        from onvif_utils.mediamtx_api import MediaMTXAPI

        def check_and_sync():
            for device in Device.objects.all():
                if not device.username or not device.password:
                    continue

                try:
                    client = OnvifClient(
                        device.host, device.port, device.username, device.password
                    )
                    client.get_device_info()
                    device.is_online = True
                    device.failure_count = 0
                except Exception:
                    device.is_online = False

                device.save(update_fields=["is_online", "failure_count"])

            mtx = MediaMTXAPI()
            for attempt in range(10):
                try:
                    mtx.list_paths()
                    break
                except Exception:
                    if attempt == 9:
                        return
                    time.sleep(2**attempt)

            for device in Device.objects.filter(is_online=True):
                if not device.stream_uris:
                    continue
                profiles = list(device.stream_uris.keys())
                uris = list(device.stream_uris.values())
                try:
                    mtx.ensure_camera_streams(device.id, profiles, uris)
                except Exception:
                    pass

            import os

            import redis
            import requests

            r = redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
            ds_url = "http://computer-vision:9000/api/v1/health/get-dsready-state"

            for attempt in range(20):
                try:
                    resp = requests.get(ds_url, timeout=5)
                    if resp.json().get("ds-ready") == "YES":
                        break
                except Exception:
                    pass
                time.sleep(3)

            try:
                requests.post(
                    f"http://computer-vision:9000/api/v1/stream/add",
                    json={
                        "key": "daemon-primer",
                        "value": {
                            "camera_id": "_primer_",
                            "camera_name": "primer",
                            "camera_url": "file:///opt/nvidia/deepstream/deepstream/samples/streams/sample_1080p_h264.mp4",
                            "change": "camera_add",
                        },
                    },
                    timeout=5,
                )
            except Exception:
                pass

            for device in Device.objects.filter(is_online=True):
                if not device.stream_uris or not device.default_profile_token:
                    continue
                uri = device.stream_uris.get(device.default_profile_token)
                if not uri:
                    continue
                clean = uri.split("&unicast=true")[0]
                payload = {
                    "action": "start_preview",
                    "device_id": device.id,
                    "camera_id": str(device.id),
                    "rtsp_uri": clean,
                    "camera_name": device.name,
                }
                r.publish("deepstream:commands", json.dumps(payload))

        thread = threading.Thread(target=check_and_sync, daemon=True)
        thread.start()
