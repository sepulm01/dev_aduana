import base64
import json
from datetime import datetime, timezone

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404
from devices.models import Device, AnalyticsPreset
from onvif_utils.client import OnvifClient
from onvif_utils.ptz import PTZService
from onvif_utils.snapshot import capture_frame_rtsp


@login_required
@csrf_exempt
def move(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "JSON required"}, status=400)

    profile_token = data.get("profile_token")
    if not profile_token:
        return JsonResponse({"error": "profile_token required"}, status=400)

    client = OnvifClient(device.host, device.port, device.username, device.password)
    svc = PTZService(client)

    move_type = data.get("type", "absolute")
    pan = float(data.get("pan", 0))
    tilt = float(data.get("tilt", 0))
    zoom = float(data.get("zoom", 0))

    try:
        if move_type == "absolute":
            svc.absolute_move(profile_token, pan, tilt, zoom)
        elif move_type == "continuous":
            svc.continuous_move(profile_token, pan, tilt, zoom)
        elif move_type == "stop":
            svc.stop(profile_token)
        else:
            return JsonResponse({"error": f"unknown type: {move_type}"}, status=400)
        return JsonResponse({"ok": True})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@login_required
@csrf_exempt
def status(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    profile_token = request.GET.get("profile_token")
    if not profile_token:
        return JsonResponse({"error": "profile_token required"}, status=400)

    try:
        client = OnvifClient(device.host, device.port, device.username, device.password)
        svc = PTZService(client)
        status_data = svc.get_status(profile_token)
        presets_data = svc.get_presets(profile_token)

        result = {"ptz_supported": True, "status": {}, "move_status": {}, "presets": []}

        if status_data and hasattr(status_data, "Position") and status_data.Position:
            pos = status_data.Position
            result["status"] = {
                "pan": pos.PanTilt.x if hasattr(pos, "PanTilt") and pos.PanTilt else 0,
                "tilt": pos.PanTilt.y if hasattr(pos, "PanTilt") and pos.PanTilt else 0,
                "zoom": pos.Zoom.x if hasattr(pos, "Zoom") and pos.Zoom else 0,
            }

        if status_data and hasattr(status_data, "MoveStatus"):
            ms = status_data.MoveStatus
            result["move_status"] = {
                "pan_tilt": getattr(ms, "PanTilt", "IDLE"),
                "zoom": getattr(ms, "Zoom", "IDLE"),
            }

        result["presets"] = [
            {
                "token": getattr(p, "token", "") or getattr(p, "_token", ""),
                "name": getattr(p, "Name", ""),
            }
            for p in (presets_data or [])
        ]

        return JsonResponse(result)
    except Exception as e:
        return JsonResponse({"ptz_supported": False, "error": str(e)})


@login_required
@csrf_exempt
def preset(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    if request.method == "GET":
        profile_token = request.GET.get("profile_token")
        if not profile_token:
            return JsonResponse({"error": "profile_token required"}, status=400)
        try:
            client = OnvifClient(
                device.host, device.port, device.username, device.password
            )
            svc = PTZService(client)
            presets = svc.get_presets(profile_token)
            return JsonResponse(
                [
                    {
                        "token": getattr(p, "token", "") or getattr(p, "_token", ""),
                        "name": getattr(p, "Name", ""),
                    }
                    for p in (presets or [])
                ],
                safe=False,
            )
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "JSON required"}, status=400)

    profile_token = data.get("profile_token")
    if not profile_token:
        return JsonResponse({"error": "profile_token required"}, status=400)

    action = data.get("action", "")
    if action not in ("set", "goto", "remove"):
        return JsonResponse({"error": "action must be set/goto/remove"}, status=400)

    try:
        client = OnvifClient(device.host, device.port, device.username, device.password)
        svc = PTZService(client)

        if action == "set":
            name = data.get("name", "")
            if not name:
                return JsonResponse({"error": "name required for set"}, status=400)
            token = svc.set_preset(profile_token, name)

            current_pos = {}
            try:
                status_data = svc.get_status(profile_token)
                if status_data and hasattr(status_data, "Position") and status_data.Position:
                    pos = status_data.Position
                    if hasattr(pos, "PanTilt") and pos.PanTilt:
                        current_pos["pan"] = pos.PanTilt.x
                        current_pos["tilt"] = pos.PanTilt.y
                    if hasattr(pos, "Zoom") and pos.Zoom:
                        current_pos["zoom"] = pos.Zoom.x
            except Exception:
                pass

            stream_uri = device.stream_uris.get(profile_token, "")
            if stream_uri:
                try:
                    frame_bytes = capture_frame_rtsp(stream_uri, timeout=10)
                    snapshot_b64 = base64.b64encode(frame_bytes).decode()
                    AnalyticsPreset.objects.update_or_create(
                        device=device,
                        preset_token=token,
                        defaults={
                            "preset_name": name,
                            "snapshot": snapshot_b64,
                            "ptz_position": current_pos,
                        },
                    )
                except Exception as snap_e:
                    print(
                        f"Snapshot capture failed for device {device.id} preset {token}: {snap_e}"
                    )

            return JsonResponse({"ok": True, "preset_token": token})

        elif action == "goto":
            preset_token = data.get("preset_token", "")
            if not preset_token:
                return JsonResponse(
                    {"error": "preset_token required for goto"}, status=400
                )
            speed = float(data.get("speed", 1.0))
            svc.goto_preset(profile_token, preset_token, speed)
            return JsonResponse({"ok": True})

        elif action == "remove":
            preset_token = data.get("preset_token", "")
            if not preset_token:
                return JsonResponse(
                    {"error": "preset_token required for remove"}, status=400
                )
            svc.remove_preset(profile_token, preset_token)
            return JsonResponse({"ok": True})

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
