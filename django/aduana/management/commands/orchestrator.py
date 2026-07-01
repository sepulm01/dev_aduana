import json
import logging
import os
from datetime import datetime, timezone as dt_timezone

import redis
from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger("aduana.orchestrator")

EVENT_WINDOW_SECONDS = 3
EVENT_IDLE_SECONDS = 10


class AduanaOrchestrator:
    def __init__(self):
        redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
        self._redis = redis.from_url(redis_url)
        self._pubsub = self._redis.pubsub()
        self._running = False

    def start(self):
        import django
        django.setup()

        self._pubsub.psubscribe("device:*:detections")
        self._running = True
        logger.info("Orchestrator started, listening on device:*:detections")

        while self._running:
            try:
                msg = self._pubsub.get_message(timeout=1.0)
                if msg and msg["type"] == "pmessage":
                    self._handle_detection(msg["data"])
            except Exception as e:
                if self._running:
                    logger.error("Orchestrator error: %s", e)

    def stop(self):
        self._running = False
        self._pubsub.close()

    def _handle_detection(self, data):
        from aduana.models import ContainerEvent

        try:
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            payload = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        device_id = payload.get("device_id")
        source_id = payload.get("source_id")
        timestamp_ms = payload.get("timestamp_ms")
        objects = payload.get("objects", [])

        if not device_id or not timestamp_ms:
            return

        ts = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=dt_timezone.utc)

        has_objects = len(objects) > 0

        if has_objects:
            window_start = ts - __import__('datetime').timedelta(seconds=EVENT_WINDOW_SECONDS)
            event = (
                ContainerEvent.objects
                .filter(
                    seal_status="processing",
                    timestamp_start__gte=window_start,
                )
                .order_by("-timestamp_start")
                .first()
            )

            if not event:
                event = ContainerEvent.objects.create(
                    seal_status="processing",
                    timestamp_start=ts,
                )
                logger.info("New container event %s started", event.id)


class Command(BaseCommand):
    help = "Orchestrator: correlates detections from both camera streams"

    def handle(self, **options):
        orchestrator = AduanaOrchestrator()
        try:
            orchestrator.start()
        except KeyboardInterrupt:
            orchestrator.stop()
