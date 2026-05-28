import http.client
import logging
import os
import socket

import redis
from django.apps import apps

logger = logging.getLogger(__name__)

CONFIG_YML_PATH = os.environ.get(
    "CONFIG_YML_PATH", "/opt/computer_vision/config/config.yml"
)
COMPUTER_VISION_CONTAINER = "mediamtx-manager-computer-vision-1"


def _get_redis():
    return redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))


def restart_computer_vision():
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(15)
        sock.connect("/var/run/docker.sock")
        conn = http.client.HTTPConnection("localhost")
        conn.sock = sock
        conn.request("POST", f"/containers/{COMPUTER_VISION_CONTAINER}/restart")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        if resp.status == 204:
            logger.info("Container computer-vision restarted successfully")
        else:
            logger.error("Docker restart returned status %d", resp.status)
    except Exception as e:
        logger.error("Failed to restart computer-vision: %s", e)


def regenerate_config_and_restart():
    from devices.config_generator import generate_config, generate_nvdsanalytics_config
    from onvif_utils.mediamtx_api import MediaMTXAPI

    Device = apps.get_model("devices", "Device")

    online_devices = list(
        Device.objects.filter(
            is_online=True, stream_uris__isnull=False
        ).exclude(stream_uris={})
    )

    if not online_devices:
        logger.warning("No online devices with stream URIs, skipping config regeneration")
        return

    mtx = MediaMTXAPI()
    for device in online_devices:
        uri = device.stream_uris.get(device.default_profile_token, "")
        if uri:
            try:
                mtx.ensure_camera_streams(
                    device.id, [device.default_profile_token], [uri]
                )
            except Exception:
                pass

    uris = generate_config(online_devices, CONFIG_YML_PATH)

    config_dir = os.path.dirname(CONFIG_YML_PATH)
    generate_nvdsanalytics_config(online_devices, config_dir)

    r = _get_redis()
    for i, device in enumerate(online_devices):
        uri = uris[i] if i < len(uris) else ""
        r.hset("deepstream:sources", str(i), str(device.id))
        r.hset("deepstream:sources", f"{i}:camera_id", str(device.id))
        r.hset("deepstream:sources", f"{i}:url", uri)

    logger.info("Config regenerated for %d online devices", len(online_devices))
    restart_computer_vision()
