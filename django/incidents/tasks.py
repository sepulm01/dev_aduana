import logging
from datetime import datetime, timezone

from celery import shared_task

logger = logging.getLogger("incidents.tasks")


@shared_task
def incident_manager():
    try:
        from incidents.models import Incident, IncidentLog, EscalationLevel

        active = Incident.objects.filter(status="active").select_related(
            "incident_type", "device"
        )

        for incident in active:
            try:
                _process_incident(incident)
            except Exception as e:
                logger.warning("Error processing incident %s: %s", incident.id, e)

        _auto_resolve()

    except Exception as e:
        logger.warning("incident_manager error: %s", e)


def _process_incident(incident):
    now = datetime.now(timezone.utc)
    itype = incident.incident_type

    current_level_obj = itype.levels.filter(level=incident.current_level).first()
    if current_level_obj is None:
        return

    elapsed = (now - incident.level_started_at).total_seconds()

    if elapsed < current_level_obj.timeout_seconds:
        _send_level_notification_if_needed(incident, current_level_obj)
    else:
        _escalate(incident, itype, current_level_obj)


def _send_level_notification_if_needed(incident, level_obj):
    from incidents.models import IncidentLog

    already_notified = IncidentLog.objects.filter(
        incident=incident,
        level=incident.current_level,
        action="notified",
    ).exists()
    if already_notified:
        return

    success = _send_notification(incident, level_obj)
    IncidentLog.objects.create(
        incident=incident,
        level=incident.current_level,
        action="notified",
        success=success,
        detail={
            "channel_type": level_obj.channel.channel_type,
            "channel_name": level_obj.channel.name,
        },
    )


def _send_notification(incident, level_obj):
    try:
        from notifications.backends import get_backend

        backend = get_backend(level_obj.channel.channel_type)
        device_name = incident.device.name if incident.device else "Unknown"

        if level_obj.message_template:
            context = {
                "device_name": device_name,
                "device_id": incident.device_id,
                "incident_id": incident.id,
                "incident_type": incident.incident_type.name,
                "level": incident.current_level,
                "code": incident.event_data.get("code", ""),
                "action": incident.event_data.get("action", ""),
            }
            message = backend.format_message(level_obj.message_template, context)
        else:
            message = _build_escalation_message(incident, level_obj)

        if level_obj.requires_ack and level_obj.channel.channel_type == "telegram":
            callback_data = f"incident_{incident.id}"
            reply_markup = {
                "inline_keyboard": [
                    [
                        {"text": "Atender alerta", "callback_data": f"ack_{callback_data}"},
                        {"text": "Falsa alarma", "callback_data": f"false_{callback_data}"},
                    ]
                ]
            }
            message_id = backend.send_with_reply_markup(level_obj.channel, message, reply_markup)
            if message_id:
                from django.core.cache import cache

                cache.set(
                    f"tg_msg:{message_id}",
                    incident.id,
                    timeout=level_obj.timeout_seconds + 300,
                )
            return message_id is not None
        else:
            return backend.send(level_obj.channel, message)

    except Exception as e:
        logger.warning("Send notification error: %s", e)
        return False


def _build_escalation_message(incident, level_obj):
    device_name = incident.device.name if incident.device else "Unknown"
    lines = [
        f"Nivel {incident.current_level} - {incident.incident_type.name}",
        f"Dispositivo: {device_name}",
        f"Incidente: #{incident.id}",
    ]
    code = incident.event_data.get("code", "")
    action = incident.event_data.get("action", "")
    if code:
        lines.append(f"Evento: {code} {action}".strip())
    data = incident.event_data.get("data", {})
    if data:
        analytics = data.get("analytics", {})
        if analytics:
            for k, v in analytics.items():
                lines.append(f"{k}: {v}")
    return "\n".join(lines)


def _escalate(incident, itype, current_level_obj):
    from incidents.models import IncidentLog

    next_level_obj = itype.levels.filter(level__gt=incident.current_level).order_by("level").first()

    if next_level_obj is None:
        incident.status = "expired"
        incident.resolved_at = datetime.now(timezone.utc)
        incident.save(update_fields=["status", "resolved_at"])
        IncidentLog.objects.create(
            incident=incident,
            level=incident.current_level,
            action="expired",
            detail={"reason": "no more levels"},
        )
        return

    IncidentLog.objects.create(
        incident=incident,
        level=incident.current_level,
        action="escalated",
        detail={"from_level": incident.current_level, "to_level": next_level_obj.level},
    )

    incident.current_level = next_level_obj.level
    incident.level_started_at = datetime.now(timezone.utc)
    incident.save(update_fields=["current_level", "level_started_at"])

    success = _send_notification(incident, next_level_obj)
    IncidentLog.objects.create(
        incident=incident,
        level=next_level_obj.level,
        action="notified",
        success=success,
    )


def _auto_resolve():
    from incidents.models import Incident, IncidentLog

    now = datetime.now(timezone.utc)
    active = Incident.objects.filter(status="active").select_related("incident_type")

    for incident in active:
        itype = incident.incident_type
        if itype.auto_resolve_seconds <= 0:
            continue
        elapsed = (now - incident.created_at).total_seconds()
        if elapsed >= itype.auto_resolve_seconds:
            incident.status = "resolved"
            incident.resolved_at = now
            incident.save(update_fields=["status", "resolved_at"])
            IncidentLog.objects.create(
                incident=incident,
                level=incident.current_level,
                action="resolved",
                detail={"reason": "auto_resolve"},
            )
