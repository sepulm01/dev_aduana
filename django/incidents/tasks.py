import logging
from datetime import datetime, timezone

from celery import shared_task

logger = logging.getLogger("incidents.tasks")


@shared_task
def incident_manager():
    try:
        from incidents.models import Incident, IncidentLog

        active = Incident.objects.filter(status="active").select_related(
            "incident_type", "device", "device__site"
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
    site = getattr(incident.device, "site", None)
    if site is None:
        _expire_no_site(incident)
        return

    escalation_configs = list(
        site.escalation_levels.order_by("level")
    )
    if not escalation_configs:
        _expire_no_site(incident)
        return

    now = datetime.now(timezone.utc)
    current_config = next(
        (c for c in escalation_configs if c.level == incident.current_level), None
    )

    if current_config is None:
        next_config = next(
            (c for c in escalation_configs if c.level > incident.current_level), None
        )
        if next_config:
            _log_escalated(incident, incident.current_level, next_config.level)
            incident.current_level = next_config.level
            incident.level_started_at = now
            incident.save(update_fields=["current_level", "level_started_at"])
            _notify_level(incident, site, next_config)
        else:
            _expire(incident)
        return

    elapsed = (now - incident.level_started_at).total_seconds()

    if elapsed < current_config.timeout_seconds:
        _notify_level_if_needed(incident, site, current_config)
    else:
        next_config = next(
            (c for c in escalation_configs if c.level > current_config.level), None
        )
        if next_config:
            _log_escalated(incident, current_config.level, next_config.level)
            incident.current_level = next_config.level
            incident.level_started_at = now
            incident.save(update_fields=["current_level", "level_started_at"])
            _notify_level(incident, site, next_config)
        else:
            _expire(incident)


def _notify_level_if_needed(incident, site, config):
    from incidents.models import IncidentLog

    already_notified = IncidentLog.objects.filter(
        incident=incident,
        level=config.level,
        action__in=["notified", "notified_user"],
    ).exists()
    if already_notified:
        return

    _notify_level(incident, site, config)


def _notify_level(incident, site, config):
    from incidents.models import IncidentLog

    notified = False

    for channel in site.channels.filter(is_active=True):
        if _send_to_channel(channel, incident, config):
            notified = True
            IncidentLog.objects.create(
                incident=incident,
                level=config.level,
                action="notified",
                success=True,
                detail={"channel_type": channel.channel_type, "channel_name": channel.name},
            )

    from operadores.models import SiteMembership

    memberships = SiteMembership.objects.filter(
        site=site,
        is_active=True,
        user__profile__escalation_level=config.level,
    ).select_related("user__profile")

    for membership in memberships:
        profile = membership.user.profile
        for channel in profile.personal_channels.filter(is_active=True):
            if _send_to_channel(channel, incident, config, user=membership.user):
                notified = True
                IncidentLog.objects.create(
                    incident=incident,
                    level=config.level,
                    action="notified_user",
                    success=True,
                    detail={
                        "channel_type": channel.channel_type,
                        "channel_name": channel.name,
                        "user": membership.user.username,
                    },
                )

    if not notified:
        IncidentLog.objects.create(
            incident=incident,
            level=config.level,
            action="notified",
            success=False,
            detail={"reason": "no channels available"},
        )


def _send_to_channel(channel, incident, config, user=None):
    try:
        from notifications.backends import get_backend

        backend = get_backend(channel.channel_type)
        device_name = incident.device.name if incident.device else "Unknown"

        message = _build_escalation_message(incident, config, user)

        if config.requires_ack and channel.channel_type == "telegram":
            callback_data = str(incident.id)
            reply_markup = {
                "inline_keyboard": [
                    [
                        {"text": "Atender alerta", "callback_data": f"ack_{callback_data}"},
                        {"text": "Falsa alarma", "callback_data": f"false_{callback_data}"},
                    ]
                ]
            }
            message_id = backend.send_with_reply_markup(channel, message, reply_markup)
            if message_id:
                from django.core.cache import cache

                cache.set(
                    f"tg_msg:{message_id}",
                    incident.id,
                    timeout=config.timeout_seconds + 300,
                )
            return message_id is not None
        else:
            return backend.send(channel, message)

    except Exception as e:
        logger.warning("Send notification error for channel %s: %s", channel.name, e)
        return False


def _build_escalation_message(incident, config, user=None):
    device_name = incident.device.name if incident.device else "Unknown"
    site_name = getattr(getattr(incident.device, "site", None), "name", "N/A")
    lines = [
        f"Nivel {config.level} - {incident.incident_type.name}",
        f"Site: {site_name}",
        f"Dispositivo: {device_name}",
        f"Incidente: #{incident.id}",
    ]
    if user:
        lines.append(f"Operador: {user.username}")
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


def _log_escalated(incident, from_level, to_level):
    from incidents.models import IncidentLog

    IncidentLog.objects.create(
        incident=incident,
        level=from_level,
        action="escalated",
        detail={"from_level": from_level, "to_level": to_level},
    )


def _expire(incident):
    from incidents.models import IncidentLog

    now = datetime.now(timezone.utc)
    incident.status = "expired"
    incident.resolved_at = now
    incident.save(update_fields=["status", "resolved_at"])
    IncidentLog.objects.create(
        incident=incident,
        level=incident.current_level,
        action="expired",
        detail={"reason": "no more levels"},
    )


def _expire_no_site(incident):
    from incidents.models import IncidentLog

    now = datetime.now(timezone.utc)
    incident.status = "expired"
    incident.resolved_at = now
    incident.save(update_fields=["status", "resolved_at"])
    IncidentLog.objects.create(
        incident=incident,
        level=incident.current_level,
        action="expired",
        detail={"reason": "device has no site assigned"},
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
