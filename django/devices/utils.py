import http.client
import logging
import os
import socket

import redis
from django.apps import apps

logger = logging.getLogger(__name__)

CONFIG_YML_PATH = os.environ.get(
    "CONFIG_YML_PATH", "/opt/computer_vision/config/config_aduana.yml"
)
COMPUTER_VISION_CONTAINER = "aduana-computer-vision-aduana-1"

PIPELINE_INSTANCES = {
    "aduana": [
        "aduana-computer-vision-aduana-1",
    ],
}


def _get_redis():
    return redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))


def restart_computer_vision(container_name=None):
    if container_name is None:
        container_name = COMPUTER_VISION_CONTAINER
    _docker_control(container_name, "restart")


def _docker_control(container_name, action):
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(15)
        sock.connect("/var/run/docker.sock")
        conn = http.client.HTTPConnection("localhost")
        conn.sock = sock
        conn.request("POST", f"/containers/{container_name}/{action}")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        if resp.status in (204, 304):
            logger.info("Container %s %s successfully", container_name, action)
        else:
            logger.error("Docker %s returned status %d for %s", action, resp.status, container_name)
    except Exception as e:
        logger.error("Failed to %s container %s: %s", action, container_name, e)


def regenerate_config_and_restart(pipeline_id=None):
    from devices.config_generator import (
        MAX_INSTANCES,
        PIPELINE_CONFIGS,
        generate_all_configs,
        get_pipeline_filename,
    )
    from onvif_utils.mediamtx_api import MediaMTXAPI

    Device = apps.get_model("devices", "Device")

    online_devices = list(
        Device.objects.filter(
            is_online=True, stream_uris__isnull=False, source_type="rtsp"
        ).exclude(stream_uris={})
    )
    file_devices = list(
        Device.objects.filter(
            stream_uris__isnull=False, source_type="file"
        ).exclude(stream_uris={})
    )

    all_devices = online_devices + file_devices

    if not all_devices:
        logger.warning("No devices with stream URIs, stopping pipeline container")
        for container_name in PIPELINE_INSTANCES["aduana"]:
            _docker_control(container_name, "stop")
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

    for device in file_devices:
        try:
            mtx.ensure_file_stream(device)
        except Exception:
            pass

    config_dir = os.path.dirname(CONFIG_YML_PATH)
    generate_all_configs(config_dir)

    r = _get_redis()
    for pipeline_id_key in PIPELINE_INSTANCES:
        pipeline_devices = [
            d for d in all_devices if d.deepstream_pipeline == pipeline_id_key
        ]
        pipeline_cfg = PIPELINE_CONFIGS[pipeline_id_key]
        max_per_instance = pipeline_cfg["max_devices_per_instance"]
        instances_needed = min(
            max((len(pipeline_devices) + max_per_instance - 1) // max_per_instance, 1),
            MAX_INSTANCES,
        )

        for n in range(MAX_INSTANCES):
            instance = n + 1
            container_name = PIPELINE_INSTANCES[pipeline_id_key][n]
            sources_key = f"deepstream:sources:{pipeline_id_key}:{instance}"

            r.delete(sources_key)

            if instance <= instances_needed and pipeline_devices:
                my_devices = pipeline_devices[n :: instances_needed]
                for idx, device in enumerate(my_devices):
                    uri = device.stream_uris.get(device.default_profile_token, "")
                    r.hset(sources_key, str(idx), str(device.id))
                    r.hset(sources_key, f"{idx}:camera_id", str(device.id))
                    r.hset(sources_key, f"{idx}:url", uri)

                if pipeline_id and pipeline_id != pipeline_id_key:
                    continue
                _docker_control(container_name, "restart")
            else:
                _docker_control(container_name, "stop")

    logger.info("Configs regenerated for %d devices", len(all_devices))
