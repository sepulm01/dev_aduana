import json
import os

import redis
from urllib.parse import urlparse

from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.conf import settings
from devices.models import Device, AnalyticsPreset

DEFAULT_CAMERA_SPECS = {
    "h_fov_wide": 99.1,
    "h_fov_tele": 31.9,
    "v_fov_wide": 53.4,
    "v_fov_tele": 18.0,
    "pan_range": 355,
    "tilt_range": 90,
}


def build_stream_context(device, profile_token, host_header=None):
    is_file_source = getattr(device, "source_type", "rtsp") == "file"

    stream_name = ""
    webrtc_url = ""
    if profile_token:
        suffix = "" if is_file_source else "_hw"
        stream_name = f"cam_{device.id}_{profile_token}{suffix}"
        webrtc_url = f"/stream/{stream_name}/"

    specs = {**DEFAULT_CAMERA_SPECS, **(device.camera_specs or {})}

    specs_obj = device.camera_specs or {}
    has_ptz_caps = bool(isinstance(specs_obj, dict) and specs_obj.get("ptz_caps"))

    return {
        "stream_name": stream_name,
        "webrtc_url": webrtc_url,
        "camera_specs_json": json.dumps(specs),
        "ptz_supported": has_ptz_caps,
        "is_file_source": is_file_source,
    }


@login_required
def live_view(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    profile_token = request.GET.get("profile") or device.default_profile_token or ""
    ctx = build_stream_context(device, profile_token, request.get_host())
    ctx["device"] = device
    ctx["profile_token"] = profile_token

    try:
        r = redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
        val = r.get(f"device:{device.id}:active_preset")
        token = val.decode() if val else ""
        ctx["active_preset"] = token
        preset_name = ""
        if token:
            ap = AnalyticsPreset.objects.filter(
                device=device, preset_token=token
            ).first()
            preset_name = ap.preset_name if ap else ""
        ctx["active_preset_name"] = preset_name
    except Exception:
        ctx["active_preset"] = ""
        ctx["active_preset_name"] = ""

    ctx["device_rules"] = []

    return render(request, "live/live.html", ctx)
