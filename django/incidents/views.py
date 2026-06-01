import json
import logging

from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required

from incidents.models import IncidentType, EscalationLevel, Incident, IncidentLog
from devices.models import Device
from notifications.models import NotificationChannel

logger = logging.getLogger(__name__)


@login_required
def incident_type_list(request):
    types = IncidentType.objects.prefetch_related("levels__channel").all()
    return render(request, "incidents/incident_type_list.html", {"incident_types": types})


@login_required
@csrf_exempt
def incident_type_create(request):
    channels = NotificationChannel.objects.filter(is_active=True)
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
        for level_data in data.get("levels", []):
            EscalationLevel.objects.create(
                incident_type=itype,
                level=level_data.get("level", 1),
                channel_id=level_data["channel_id"],
                timeout_seconds=level_data.get("timeout_seconds", 60),
                requires_ack=level_data.get("requires_ack", True),
                message_template=level_data.get("message_template", ""),
                auto_actions=level_data.get("auto_actions", []),
            )
        return JsonResponse({"ok": True, "id": itype.id})
    return render(request, "incidents/incident_type_form.html", {
        "incident_type": None,
        "channels": channels,
    })


@login_required
@csrf_exempt
def incident_type_edit(request, type_id):
    itype = get_object_or_404(IncidentType, id=type_id)
    channels = NotificationChannel.objects.filter(is_active=True)
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
        if "levels" in data:
            itype.levels.all().delete()
            for level_data in data["levels"]:
                EscalationLevel.objects.create(
                    incident_type=itype,
                    level=level_data.get("level", 1),
                    channel_id=level_data["channel_id"],
                    timeout_seconds=level_data.get("timeout_seconds", 60),
                    requires_ack=level_data.get("requires_ack", True),
                    message_template=level_data.get("message_template", ""),
                    auto_actions=level_data.get("auto_actions", []),
                )
        return JsonResponse({"ok": True})
    return render(request, "incidents/incident_type_form.html", {
        "incident_type": itype,
        "channels": channels,
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
