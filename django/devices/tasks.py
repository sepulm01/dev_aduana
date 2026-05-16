import logging
from datetime import datetime, timezone

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from celery import shared_task

from devices.models import Device
from onvif_utils.drivers import get_driver
from onvif_utils.drivers.base import DriverError

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=5)
def poll_camera_motion(self, device_id):
    try:
        device = Device.objects.get(id=device_id)
    except Device.DoesNotExist:
        logger.warning("Device %s not found, skipping motion poll", device_id)
        return

    driver = get_driver(device)
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
    if motion_active != device.motion_active:
        device.motion_active = motion_active
        device.save(update_fields=["motion_active"])

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
