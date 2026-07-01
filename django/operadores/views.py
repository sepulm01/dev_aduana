import json
import logging

from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required

from operadores.models import Site, SiteEscalationLevel, OperatorProfile, SiteMembership
from devices.models import Device
from django.contrib.auth.models import User

logger = logging.getLogger(__name__)


@login_required
def site_list(request):
    sites = Site.objects.prefetch_related("escalation_levels").all()
    return render(request, "operadores/site_list.html", {"sites": sites})


@login_required
@csrf_exempt
def site_create(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "JSON invalido"}, status=400)
        name = data.get("name", "").strip()
        if not name:
            return JsonResponse({"error": "name requerido"}, status=400)
        site = Site.objects.create(
            name=name,
            description=data.get("description", ""),
            is_active=data.get("is_active", True),
        )
        for level_data in data.get("levels", []):
            SiteEscalationLevel.objects.create(
                site=site,
                level=level_data.get("level", 1),
                timeout_seconds=level_data.get("timeout_seconds", 60),
                requires_ack=level_data.get("requires_ack", True),
            )
        return JsonResponse({"ok": True, "id": site.id})
    return render(request, "operadores/site_form.html", {"site": None})


@login_required
@csrf_exempt
def site_edit(request, site_id):
    site = get_object_or_404(Site, id=site_id)
    if request.method == "POST":
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "JSON invalido"}, status=400)
        site.name = data.get("name", site.name)
        site.description = data.get("description", site.description)
        site.is_active = data.get("is_active", site.is_active)
        site.save()
        if "levels" in data:
            site.escalation_levels.all().delete()
            for level_data in data["levels"]:
                SiteEscalationLevel.objects.create(
                    site=site,
                    level=level_data.get("level", 1),
                    timeout_seconds=level_data.get("timeout_seconds", 60),
                    requires_ack=level_data.get("requires_ack", True),
                )
        return JsonResponse({"ok": True})
    return render(request, "operadores/site_form.html", {"site": site})


@login_required
@csrf_exempt
def site_delete(request, site_id):
    site = get_object_or_404(Site, id=site_id)
    if request.method == "POST":
        site.delete()
        return JsonResponse({"ok": True})
    return JsonResponse({"error": "POST required"}, status=405)


@login_required
def profile_view(request):
    profile = get_object_or_404(OperatorProfile, user=request.user)
    sites = Site.objects.filter(is_active=True)
    return render(request, "operadores/profile.html", {
        "profile": profile,
        "all_sites": sites,
    })


@login_required
@csrf_exempt
def profile_edit(request):
    profile = get_object_or_404(OperatorProfile, user=request.user)
    if request.method == "POST":
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "JSON invalido"}, status=400)
        profile.phone_number = data.get("phone_number", profile.phone_number)
        profile.cargo = data.get("cargo", profile.cargo)
        profile.escalation_level = data.get("escalation_level", profile.escalation_level)
        profile.save()
        return JsonResponse({"ok": True})
    return JsonResponse({"error": "POST required"}, status=405)


@login_required
def operator_list(request):
    profiles = OperatorProfile.objects.select_related("user").all()
    return render(request, "operadores/operator_list.html", {"profiles": profiles})


@login_required
@csrf_exempt
def operator_edit(request, user_id):
    profile = get_object_or_404(OperatorProfile, user_id=user_id)
    all_sites = Site.objects.filter(is_active=True)
    if request.method == "POST":
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "JSON invalido"}, status=400)
        profile.phone_number = data.get("phone_number", profile.phone_number)
        profile.cargo = data.get("cargo", profile.cargo)
        profile.escalation_level = data.get("escalation_level", profile.escalation_level)
        profile.save()
        if "site_ids" in data:
            SiteMembership.objects.filter(user_id=user_id).delete()
            for site_id in data["site_ids"]:
                SiteMembership.objects.get_or_create(user_id=user_id, site_id=site_id)
        return JsonResponse({"ok": True})
    return render(request, "operadores/operator_form.html", {
        "profile": profile,
        "all_sites": all_sites,
    })


@login_required
@csrf_exempt
def device_assign_site(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    if request.method == "POST":
        data = json.loads(request.body) if request.body else {}
        site_id = data.get("site_id")
        if site_id:
            device.site = get_object_or_404(Site, id=site_id)
        else:
            device.site = None
        device.save(update_fields=["site"])
        return JsonResponse({"ok": True, "site_id": device.site_id})
    return JsonResponse({"error": "POST required"}, status=405)
