import logging
from datetime import datetime, timezone

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from celery import shared_task
from django.db import transaction

from devices.models import Device
from onvif_utils.drivers import get_driver
from onvif_utils.drivers.base import DriverError

logger = logging.getLogger(__name__)

MAX_FAILURE_COUNT = 3


def _broadcast_device_status(device_id, online):
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"device_{device_id}",
        {
            "type": "device_status",
            "device_id": str(device_id),
            "online": online,
        },
    )


@shared_task(bind=True, max_retries=3, default_retry_delay=5)
def poll_camera_motion(self, device_id):
    with transaction.atomic():
        try:
            device = Device.objects.select_for_update().get(id=device_id)
        except Device.DoesNotExist:
            logger.warning("Device %s not found, skipping motion poll", device_id)
            return

        driver = get_driver(device)
        ping_result = driver.ping()
        online = ping_result["online"]
        last_seen = ping_result["last_seen"]

        status_changed = False
        if online:
            if device.failure_count > 0 or not device.is_online:
                device.failure_count = 0
                if not device.is_online:
                    device.is_online = True
                    status_changed = True
            if last_seen:
                device.last_seen = last_seen
            device.save(update_fields=["is_online", "failure_count", "last_seen"])
            if status_changed:
                _broadcast_device_status(device_id, True)
        else:
            device.failure_count += 1
            if device.failure_count >= MAX_FAILURE_COUNT and device.is_online:
                device.is_online = False
                status_changed = True
            device.save(update_fields=["failure_count", "is_online"])
            if status_changed:
                _broadcast_device_status(device_id, False)

        device.refresh_from_db()
        current_motion_active = device.motion_active

    try:
        result = driver.poll_motion()
    except DriverError as e:
        logger.warning("Error polling motion for device %s: %s", device_id, e)
        return
    except Exception as e:
        logger.warning(
            "Unexpected error polling motion for device %s: %s", device_id, e
        )
        return

    if result is None:
        return

    motion_active = result["motion"]
    if motion_active != current_motion_active:
        Device.objects.filter(id=device_id).update(motion_active=motion_active)

        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"device_{device_id}",
            {
                "type": "motion_event",
                "device_id": str(device_id),
                "timestamp": result.get(
                    "timestamp", datetime.now(timezone.utc).isoformat()
                ),
                "metadata": {
                    "motion": motion_active,
                    **(result.get("metadata") or {}),
                },
            },
        )


@shared_task
def poll_all_cameras():
    for device in Device.objects.all():
        poll_camera_motion.delay(device.id)


@shared_task
def heartbeat_deepstream_streams():
    import json
    import os

    import redis

    r = redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
    for device in Device.objects.filter(is_online=True, deepstream_enabled=True):
        if not device.stream_uris or not device.default_profile_token:
            continue
        uri = device.stream_uris.get(device.default_profile_token)
        if not uri:
            continue
        clean = uri.split("&unicast=true")[0]
        r.publish(
            "deepstream:commands",
            json.dumps(
                {
                    "action": "stop_preview",
                    "device_id": device.id,
                    "camera_id": str(device.id),
                }
            ),
        )
        r.publish(
            "deepstream:commands",
            json.dumps(
                {
                    "action": "start_preview",
                    "device_id": device.id,
                    "camera_id": str(device.id),
                    "rtsp_uri": clean,
                    "camera_name": device.name,
                    "force": True,
                }
            ),
        )


@shared_task(bind=True, max_retries=2, default_retry_delay=10)
def refresh_device_streams(self, device_id):
    import os

    import redis

    r = redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
    lock_key = f"device:stream_refresh:{device_id}"
    if not r.set(lock_key, "1", nx=True, ex=30):
        return

    try:
        try:
            device = Device.objects.get(id=device_id)
        except Device.DoesNotExist:
            return

        if not device.username or not device.password:
            return

        from onvif_utils.client import OnvifClient
        from onvif_utils.media import MediaService
        from onvif_utils.mediamtx_api import MediaMTXAPI

        client = OnvifClient(
            device.host, device.port, device.username, device.password
        )
        svc = MediaService(client)
        profiles = svc.get_profiles()

        stream_uris = {}
        profiles_tokens = []
        for p in profiles:
            uri = svc.get_stream_uri(
                p["token"],
                username=device.username,
                password=device.password,
            )
            if uri:
                profiles_tokens.append(p["token"])
                stream_uris[p["token"]] = uri

        device._skip_stream_refresh = True
        device.stream_uris = stream_uris
        device.save(update_fields=["stream_uris"])

        mtx = MediaMTXAPI()
        mtx.ensure_camera_streams(
            device.id, profiles_tokens, list(stream_uris.values())
        )

        if profiles_tokens:
            import json as _json

            clean_uri = stream_uris[profiles_tokens[0]].split("&unicast=true")[0]
            r.publish(
                "deepstream:commands",
                _json.dumps(
                    {
                        "action": "start_preview",
                        "device_id": device_id,
                        "camera_id": str(device_id),
                        "rtsp_uri": clean_uri,
                        "camera_name": device.name,
                    }
                ),
            )

        logger.info(
            "Stream URIs refreshed for device %s: %d profiles",
            device_id,
            len(profiles_tokens),
        )
    except Exception as e:
        logger.warning(
            "Stream refresh failed for device %s: %s", device_id, e
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)
    finally:
        r.delete(lock_key)
