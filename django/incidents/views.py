import json
import logging

from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required

from incidents.models import IncidentType, Incident, IncidentLog
from devices.models import Device

logger = logging.getLogger(__name__)


@login_required
def incident_type_list(request):
    types = IncidentType.objects.all()
    return render(request, "incidents/incident_type_list.html", {"incident_types": types})


@login_required
@csrf_exempt
def incident_type_create(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "JSON invalido"}, status=400)
        name = data.get("name", "").strip()
        if not name:
            return JsonResponse({"error": "name requerido"}, status=400)
        itype = IncidentType.objects.create(
            name=name,
            description=data.get("description", ""),
            is_active=data.get("is_active", True),
            auto_resolve_seconds=data.get("auto_resolve_seconds", 0),
            dedup_window_seconds=data.get("dedup_window_seconds", 0),
        )
        return JsonResponse({"ok": True, "id": itype.id})
    return render(request, "incidents/incident_type_form.html", {
        "incident_type": None,
    })


@login_required
@csrf_exempt
def incident_type_edit(request, type_id):
    itype = get_object_or_404(IncidentType, id=type_id)
    if request.method == "POST":
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "JSON invalido"}, status=400)
        itype.name = data.get("name", itype.name)
        itype.description = data.get("description", itype.description)
        itype.is_active = data.get("is_active", itype.is_active)
        itype.auto_resolve_seconds = data.get("auto_resolve_seconds", itype.auto_resolve_seconds)
        itype.dedup_window_seconds = data.get("dedup_window_seconds", itype.dedup_window_seconds)
        itype.save()
        return JsonResponse({"ok": True})
    return render(request, "incidents/incident_type_form.html", {
        "incident_type": itype,
    })


@login_required
@csrf_exempt
def incident_type_delete(request, type_id):
    itype = get_object_or_404(IncidentType, id=type_id)
    if request.method == "POST":
        itype.delete()
        return JsonResponse({"ok": True})
    return JsonResponse({"error": "POST required"}, status=405)


@login_required
def incident_list(request):
    incidents = Incident.objects.select_related("incident_type", "device").all()[:100]
    return render(request, "incidents/incident_list.html", {"incidents": incidents})


@login_required
@csrf_exempt
def incident_ack(request, incident_id):
    incident = get_object_or_404(Incident, id=incident_id)
    if request.method == "POST":
        if incident.status != "active":
            return JsonResponse({"error": "Incident not active"}, status=400)
        from datetime import datetime, timezone

        data = json.loads(request.body) if request.body else {}
        by_whom = data.get("by", "api")
        now = datetime.now(timezone.utc)
        incident.status = "acknowledged"
        incident.acknowledged_by = by_whom
        incident.acknowledged_at = now
        incident.resolved_at = now
        incident.save(update_fields=["status", "acknowledged_by", "acknowledged_at", "resolved_at"])
        IncidentLog.objects.create(
            incident=incident,
            level=incident.current_level,
            action="acknowledged",
            detail={"by": by_whom},
        )
        return JsonResponse({"ok": True})
    return JsonResponse({"error": "POST required"}, status=405)


@login_required
def incident_dashboard(request):
    from live.views import build_stream_context
    from devices.models import Device

    active_incidents = Incident.objects.filter(
        status="active"
    ).select_related("incident_type", "device").order_by("-created_at")

    hosts = request.get_host()
    incidents_data = []
    for inc in active_incidents:
        device = inc.device
        profile_token = device.default_profile_token or ""
        ctx = {}
        if profile_token:
            ctx = build_stream_context(device, profile_token, hosts)
        incidents_data.append({
            "incident": inc,
            "webrtc_url": ctx.get("webrtc_url", ""),
        })

    all_devices = Device.objects.all()
    kpis = {
        "total_cameras": all_devices.count(),
        "online_cameras": all_devices.filter(is_online=True).count(),
        "with_analytics": all_devices.exclude(deepstream_pipeline="").count(),
        "active_incidents": active_incidents.count(),
    }

    return render(request, "incidents/dashboard.html", {
        "incidents_data": incidents_data,
        "kpis": kpis,
    })


@login_required
def incident_detail(request, incident_id):
    incident = get_object_or_404(
        Incident.objects.select_related("incident_type", "device"),
        id=incident_id,
    )
    logs = incident.logs.order_by("-timestamp")

    return render(request, "incidents/incident_detail.html", {
        "incident": incident,
        "logs": logs,
    })
