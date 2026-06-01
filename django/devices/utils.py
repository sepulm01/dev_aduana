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

PIPELINE_CONTAINERS = {
    "main": "mediamtx-manager-computer-vision-1",
    "facerec": "mediamtx-manager-computer-vision-facerec-1",
    "yolov9": "mediamtx-manager-computer-vision-yolov9-1",
}


def get_active_preset_for_device(device):
    specs = device.camera_specs or {}
    has_ptz = bool(specs.get("ptz_caps"))

    if not has_ptz:
        return device.analytics_presets.filter(preset_token="__fixed__").first()

    profile_token = device.default_profile_token
    if not profile_token:
        return None

    try:
        from onvif_utils.client import OnvifClient
        from onvif_utils.ptz import PTZService

        client = OnvifClient(device.host, device.port, device.username, device.password)
        ptz = PTZService(client)
        status = ptz.get_status(profile_token)
        presets = ptz.get_presets(profile_token)

        def get_ptz_values(s):
            if not s:
                return 0, 0, 0
            pos = getattr(s, "Position", None)
            if not pos:
                return 0, 0, 0
            pan_tilt = getattr(pos, "PanTilt", None)
            zoom = getattr(pos, "Zoom", None)
            px = getattr(pan_tilt, "x", 0) if pan_tilt else 0
            py = getattr(pan_tilt, "y", 0) if pan_tilt else 0
            z = getattr(zoom, "x", 0) if zoom else 0
            return px, py, z

        current_pan, current_tilt, current_zoom = get_ptz_values(status)

        best_match = None
        best_dist = float("inf")
        for p in presets:
            ptoken = getattr(p, "token", "") or getattr(p, "_token", "")
            preset_obj = device.analytics_presets.filter(preset_token=ptoken).first()
            if not preset_obj:
                continue
            stored_pos = getattr(preset_obj, "ptz_position", None) or {}
            pp = stored_pos.get("pan", current_pan)
            pt_val = stored_pos.get("tilt", current_tilt)
            pz = stored_pos.get("zoom", current_zoom)
            dist = (
                abs(pp - current_pan)
                + abs(pt_val - current_tilt)
                + abs(pz - current_zoom)
            )
            if dist < best_dist:
                best_dist = dist
                best_match = preset_obj

        return best_match
    except Exception as e:
        logger.warning(
            "Error determining active preset for device %s: %s", device.id, e
        )
        return None


def _get_redis():
    return redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))


def restart_computer_vision(container_name=None):
    if container_name is None:
        container_name = COMPUTER_VISION_CONTAINER
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(15)
        sock.connect("/var/run/docker.sock")
        conn = http.client.HTTPConnection("localhost")
        conn.sock = sock
        conn.request("POST", f"/containers/{container_name}/restart")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        if resp.status == 204:
            logger.info("Container %s restarted successfully", container_name)
        else:
            logger.error("Docker restart returned status %d for %s", resp.status, container_name)
    except Exception as e:
        logger.error("Failed to restart computer-vision: %s", e)


def regenerate_config_and_restart(pipeline_id=None):
    from devices.config_generator import generate_all_configs, get_pipeline_container
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

    config_dir = os.path.dirname(CONFIG_YML_PATH)
    generate_all_configs(config_dir)

    r = _get_redis()
    source_idx = 0
    for pipeline_id_key in PIPELINE_CONTAINERS:
        pipeline_devices = [d for d in online_devices if d.deepstream_pipeline == pipeline_id_key]
        for device in pipeline_devices:
            uri = device.stream_uris.get(device.default_profile_token, "")
            r.hset("deepstream:sources", str(source_idx), str(device.id))
            r.hset("deepstream:sources", f"{source_idx}:camera_id", str(device.id))
            r.hset("deepstream:sources", f"{source_idx}:url", uri)
            source_idx += 1

    logger.info("Configs regenerated for %d online devices across all pipelines", len(online_devices))

    if pipeline_id:
        restart_computer_vision(get_pipeline_container(pipeline_id))
    else:
        for pipeline_id_key in PIPELINE_CONTAINERS:
            restart_computer_vision(PIPELINE_CONTAINERS[pipeline_id_key])
