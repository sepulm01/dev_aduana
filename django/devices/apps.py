import threading
import time

from django.apps import AppConfig


class DevicesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "devices"

    def ready(self):
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

        thread = threading.Thread(target=check_and_sync, daemon=True)
        thread.start()
