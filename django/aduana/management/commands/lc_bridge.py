import json
import logging
import os
import sys

import django
import redis
from django.core.management.base import BaseCommand

logger = logging.getLogger("lc_bridge")


class Command(BaseCommand):
    help = "Subscribe to aduana:lc_event Redis channel and finalize container events"

    def handle(self, **options):
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        django.setup()

        from aduana.tasks import _finalize_event
        from aduana.models import ContainerEvent

        redis_host = os.environ.get("REDIS_HOST", "redis")
        redis_port = int(os.environ.get("REDIS_PORT", 6379))

        r = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)
        pubsub = r.pubsub()
        pubsub.subscribe("aduana:lc_event")

        logger.info("LC bridge subscribed to aduana:lc_event")

        for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                data = json.loads(message["data"])
                logger.info("LC event: %s", data)
            except json.JSONDecodeError:
                logger.warning("Invalid LC event JSON: %s", message["data"])
                continue

            window_seconds = 15
            from django.utils import timezone
            from datetime import timedelta

            window_start = timezone.now() - timedelta(seconds=window_seconds)

            event = (
                ContainerEvent.objects
                .filter(seal_status="processing", timestamp_start__gte=window_start)
                .order_by("-timestamp_start")
                .first()
            )

            if event:
                _finalize_event(event)
                logger.info(
                    "Closed event %s via line crossing (device=%s, source=%s)",
                    event.id,
                    data.get("device_id"),
                    data.get("source_id"),
                )
            else:
                logger.info("No open event found for LC event")
