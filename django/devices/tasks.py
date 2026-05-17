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
