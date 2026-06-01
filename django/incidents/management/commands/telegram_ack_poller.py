import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

from django.core.management.base import BaseCommand

logger = logging.getLogger("telegram_ack_poller")


class TelegramAckPoller:
    def __init__(self):
        self._running = False
        self._offsets = {}

    def _acknowledge_incident(self, incident_id, by_whom):
        try:
            from incidents.models import Incident, IncidentLog

            incident = Incident.objects.select_for_update().filter(
                id=incident_id, status="active"
            ).first()
            if incident is None:
                logger.info("Incident %s not found or not active", incident_id)
                return False

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
            return True
        except Exception as e:
            logger.warning("Acknowledge error: %s", e)
            return False

    def _resolve_incident_false_alarm(self, incident_id, by_whom):
        try:
            from incidents.models import Incident, IncidentLog

            incident = Incident.objects.filter(
                id=incident_id, status="active"
            ).first()
            if incident is None:
                logger.info("Incident %s not found or not active", incident_id)
                return False

            now = datetime.now(timezone.utc)
            incident.status = "resolved"
            incident.acknowledged_by = by_whom
            incident.acknowledged_at = now
            incident.resolved_at = now
            incident.save(update_fields=["status", "acknowledged_by", "acknowledged_at", "resolved_at"])

            IncidentLog.objects.create(
                incident=incident,
                level=incident.current_level,
                action="resolved",
                detail={"reason": "false_alarm", "by": by_whom},
            )
            return True
        except Exception as e:
            logger.warning("False alarm error: %s", e)
            return False

    def _handle_callback(self, callback_data, from_user, bot_token, channel):
        parts = callback_data.split("_", 1)
        if len(parts) != 2:
            return
        cmd, incident_id_str = parts
        try:
            incident_id = int(incident_id_str)
        except ValueError:
            return

        from notifications.backends.telegram import TelegramBackend

        backend = TelegramBackend()
        username = from_user.get("username", "") or from_user.get("first_name", "Unknown")

        if cmd == "ack":
            ok = self._acknowledge_incident(incident_id, username)
            response = "Alerta atendida" if ok else "La alerta ya fue atendida o expiro"
        elif cmd == "false":
            ok = self._resolve_incident_false_alarm(incident_id, username)
            response = "Marcado como falsa alarma" if ok else "La alerta ya fue procesada"
        else:
            return

        try:
            callback_id = from_user.get("id")
            if callback_id:
                backend._api_url = lambda token, method: f"https://api.telegram.org/bot{token}/{method}"
                url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                import requests

                requests.post(url, json={
                    "chat_id": callback_id,
                    "text": f"{response} (#{incident_id})",
                }, timeout=10)
        except Exception:
            pass

    def run(self):
        self._running = True
        logger.info("Telegram ack poller started")

        while self._running:
            try:
                from notifications.models import NotificationChannel

                channels = NotificationChannel.objects.filter(
                    channel_type="telegram", is_active=True
                )
                for channel in channels:
                    bot_token = channel.config.get("bot_token", "")
                    if not bot_token:
                        continue
                    self._poll_channel(channel, bot_token)
            except Exception as e:
                logger.warning("Poller error: %s", e)

            time.sleep(2)

    def _poll_channel(self, channel, bot_token):
        from notifications.backends.telegram import TelegramBackend

        backend = TelegramBackend()
        offset = self._offsets.get(bot_token)
        updates = backend.get_updates(bot_token, offset=offset)

        for update in updates:
            update_id = update.get("update_id", 0)
            if update_id >= self._offsets.get(bot_token, 0):
                self._offsets[bot_token] = update_id + 1

            callback_query = update.get("callback_query")
            if not callback_query:
                continue

            data = callback_query.get("data", "")
            from_user = callback_query.get("from", {})

            if data:
                self._handle_callback(data, from_user, bot_token, channel)

    def stop(self):
        self._running = False


class Command(BaseCommand):
    help = "Poll Telegram for acknowledgement callbacks"

    def handle(self, *args, **options):
        poller = TelegramAckPoller()

        def signal_handler(sig):
            logger.info("Received %s, shutting down", sig)
            poller.stop()
            sys.exit(0)

        try:
            signal.signal(signal.SIGTERM, lambda s, f: signal_handler(s))
            signal.signal(signal.SIGINT, lambda s, f: signal_handler(s))
        except Exception:
            pass

        poller.run()
