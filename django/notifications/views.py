import json
import logging

from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required

from notifications.models import NotificationChannel, NotificationRule
from devices.models import Device

logger = logging.getLogger(__name__)


@login_required
def channel_list(request):
    channels = NotificationChannel.objects.all()
    return render(request, "notifications/channel_list.html", {"channels": channels})


@login_required
@csrf_exempt
def channel_create(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "JSON invalido"}, status=400)
        name = data.get("name", "").strip()
        channel_type = data.get("channel_type", "").strip()
        if not name or not channel_type:
            return JsonResponse({"error": "name y channel_type requeridos"}, status=400)
        channel = NotificationChannel.objects.create(
            name=name,
            channel_type=channel_type,
            config=data.get("config", {}),
            is_active=data.get("is_active", True),
        )
        return JsonResponse({"ok": True, "id": channel.id})
    return render(request, "notifications/channel_form.html", {"channel": None})


@login_required
@csrf_exempt
def channel_edit(request, channel_id):
    channel = get_object_or_404(NotificationChannel, id=channel_id)
    if request.method == "POST":
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "JSON invalido"}, status=400)
        channel.name = data.get("name", channel.name)
        channel.channel_type = data.get("channel_type", channel.channel_type)
        channel.config = data.get("config", channel.config)
        channel.is_active = data.get("is_active", channel.is_active)
        channel.save()
        return JsonResponse({"ok": True})
    return render(request, "notifications/channel_form.html", {"channel": channel})


@login_required
@csrf_exempt
def channel_delete(request, channel_id):
    channel = get_object_or_404(NotificationChannel, id=channel_id)
    if request.method == "POST":
        channel.delete()
        return JsonResponse({"ok": True})
    return JsonResponse({"error": "POST required"}, status=405)


@login_required
def rule_list(request):
    rules = NotificationRule.objects.select_related("channel", "device").all()
    return render(request, "notifications/rule_list.html", {"rules": rules})


@login_required
@csrf_exempt
def rule_create(request):
    channels = NotificationChannel.objects.filter(is_active=True)
    devices = Device.objects.all()
    if request.method == "POST":
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "JSON invalido"}, status=400)
        name = data.get("name", "").strip()
        channel_id = data.get("channel_id")
        if not name or not channel_id:
            return JsonResponse({"error": "name y channel_id requeridos"}, status=400)
        channel = get_object_or_404(NotificationChannel, id=channel_id)
        device = None
        if data.get("device_id"):
            device = get_object_or_404(Device, id=data["device_id"])
        rule = NotificationRule.objects.create(
            name=name,
            channel=channel,
            device=device,
            event_codes=data.get("event_codes", []),
            analytics_trigger=data.get("analytics_trigger", []),
            min_objects=data.get("min_objects", 0),
            cooldown_seconds=data.get("cooldown_seconds", 0),
            min_duration_seconds=data.get("min_duration_seconds", 0),
            is_active=data.get("is_active", True),
            message_template=data.get("message_template", ""),
            send_immediate=data.get("send_immediate", True),
            send_photo=data.get("send_photo", False),
        )
        return JsonResponse({"ok": True, "id": rule.id})
    return render(request, "notifications/rule_form.html", {
        "rule": None,
        "channels": channels,
        "devices": devices,
    })


@login_required
@csrf_exempt
def rule_edit(request, rule_id):
    rule = get_object_or_404(NotificationRule, id=rule_id)
    channels = NotificationChannel.objects.filter(is_active=True)
    devices = Device.objects.all()
    if request.method == "POST":
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "JSON invalido"}, status=400)
        rule.name = data.get("name", rule.name)
        channel_id = data.get("channel_id")
        if channel_id:
            rule.channel = get_object_or_404(NotificationChannel, id=channel_id)
        device_id = data.get("device_id")
        if device_id:
            rule.device = get_object_or_404(Device, id=device_id)
        else:
            rule.device = None
        rule.event_codes = data.get("event_codes", rule.event_codes)
        rule.analytics_trigger = data.get("analytics_trigger", rule.analytics_trigger)
        rule.min_objects = data.get("min_objects", rule.min_objects)
        rule.cooldown_seconds = data.get("cooldown_seconds", rule.cooldown_seconds)
        rule.min_duration_seconds = data.get("min_duration_seconds", rule.min_duration_seconds)
        rule.is_active = data.get("is_active", rule.is_active)
        rule.message_template = data.get("message_template", rule.message_template)
        rule.send_immediate = data.get("send_immediate", rule.send_immediate)
        rule.send_photo = data.get("send_photo", rule.send_photo)
        rule.save()
        return JsonResponse({"ok": True})
    return render(request, "notifications/rule_form.html", {
        "rule": rule,
        "channels": channels,
        "devices": devices,
    })


@login_required
@csrf_exempt
def rule_delete(request, rule_id):
    rule = get_object_or_404(NotificationRule, id=rule_id)
    if request.method == "POST":
        rule.delete()
        return JsonResponse({"ok": True})
    return JsonResponse({"error": "POST required"}, status=405)
