import json
from urllib.parse import urlparse

from django.shortcuts import render, get_object_or_404
from django.conf import settings
from devices.models import Device

DEFAULT_CAMERA_SPECS = {
    "h_fov_wide": 99.1,
    "h_fov_tele": 31.9,
    "v_fov_wide": 53.4,
    "v_fov_tele": 18.0,
    "pan_range": 355,
    "tilt_range": 90,
}


def build_stream_context(device, profile_token, host_header=None):
    stream_name = f"cam_{device.id}_{profile_token}_hw" if profile_token else ""
    if profile_token:
        webrtc_url = f"/stream/{stream_name}/"
    else:
        webrtc_url = ""

    specs = {**DEFAULT_CAMERA_SPECS, **(device.camera_specs or {})}

    specs_obj = device.camera_specs or {}
    has_ptz_caps = bool(isinstance(specs_obj, dict) and specs_obj.get("ptz_caps"))

    return {
        "stream_name": stream_name,
        "webrtc_url": webrtc_url,
        "camera_specs_json": json.dumps(specs),
        "ptz_supported": has_ptz_caps,
    }


def live_view(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    profile_token = request.GET.get("profile")
    ctx = build_stream_context(device, profile_token, request.get_host())
    ctx["device"] = device
    ctx["profile_token"] = profile_token
    return render(request, "live/live.html", ctx)
