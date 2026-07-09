from django.core.paginator import Paginator
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_exempt

from aduana.models import AnalyticsPreset, ContainerEvent
from devices.models import Device

import json, logging, base64

logger = logging.getLogger(__name__)


def dashboard(request):
    events = ContainerEvent.objects.select_related().order_by("-timestamp_start")
    paginator = Paginator(events, 25)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)
    return render(request, "aduana/dashboard.html", {"page_obj": page_obj})


def event_detail(request, event_id):
    event = ContainerEvent.objects.prefetch_related("detections").get(id=event_id)
    detections = event.detections.order_by("-timestamp")
    return render(
        request,
        "aduana/event_detail.html",
        {"event": event, "detections": detections},
    )


def analytics_editor(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    profile = device.default_profile_token or ""
    return render(
        request,
        "aduana/analytics_editor.html",
        {"device": device, "default_profile": profile},
    )


@csrf_exempt
def analytics_presets(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    profile_token = request.GET.get("profile_token") or device.default_profile_token
    if not profile_token:
        return JsonResponse({"error": "profile_token required"}, status=400)

    ap = AnalyticsPreset.objects.filter(
        device=device, preset_token=profile_token
    ).first()
    return JsonResponse({
        "presets": [{"token": profile_token, "name": "Stream", "snapshot": ap.snapshot if ap else ""}],
        "has_ptz": False,
    })


@csrf_exempt
def analytics_shapes(request, device_id, preset_token):
    device = get_object_or_404(Device, id=device_id)

    if request.method == "GET":
        ap = AnalyticsPreset.objects.filter(
            device=device, preset_token=preset_token
        ).first()
        return JsonResponse({"shapes": ap.shapes if ap else [], "snapshot": ap.snapshot if ap else ""})

    if request.method == "POST":
        data = json.loads(request.body or "{}")
        shapes = data.get("shapes", [])
        ap, _ = AnalyticsPreset.objects.update_or_create(
            device=device,
            preset_token=preset_token,
            defaults={"shapes": shapes, "preset_name": "Stream"},
        )
        return JsonResponse({"status": "ok", "shapes": ap.shapes})

    return JsonResponse({"error": "Method not allowed"}, status=405)


@csrf_exempt
def analytics_capture_snapshot(request, device_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    device = get_object_or_404(Device, id=device_id)
    data = json.loads(request.body or "{}")
    preset_token = data.get("preset_token") or device.default_profile_token

    if not preset_token:
        return JsonResponse({"error": "preset_token required"}, status=400)

    from onvif import ONVIFCamera
    import socket, requests
    socket.setdefaulttimeout(15)
    WSDL = '/usr/local/lib/python3.12/site-packages/wsdl/'

    try:
        cam = ONVIFCamera(device.host, device.port, device.username, device.password, wsdl_dir=WSDL)
        media = cam.create_media_service()
        uri = media.GetSnapshotUri({'ProfileToken': preset_token})
        resp = requests.get(uri.Uri, auth=(device.username, device.password), timeout=10)
        snapshot_b64 = base64.b64encode(resp.content).decode()

        ap = AnalyticsPreset.objects.filter(
            device=device, preset_token=preset_token
        ).first()
        if ap:
            ap.snapshot = snapshot_b64
            ap.save()
        else:
            AnalyticsPreset.objects.create(
                device=device, preset_token=preset_token, snapshot=snapshot_b64
            )
        return JsonResponse({"snapshot": snapshot_b64})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
def analytics_disable(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    AnalyticsPreset.objects.filter(device=device).delete()
    from devices.config_generator import generate_nvdsanalytics_config
    generate_nvdsanalytics_config("/opt/computer_vision/config")
    return JsonResponse({"status": "ok"})


@csrf_exempt
def analytics_apply(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    from devices.config_generator import generate_nvdsanalytics_config
    generate_nvdsanalytics_config("/opt/computer_vision/config")
    return JsonResponse({"status": "ok", "message": "Config regenerated"})
