import json
import os

import redis
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Send commands to DeepStream service via Redis"

    def add_arguments(self, parser):
        parser.add_argument(
            "action", choices=["add", "remove", "status"], help="Command action"
        )
        parser.add_argument("camera_id", type=int, help="Camera device ID")
        parser.add_argument(
            "--rtsp-uri",
            dest="rtsp_uri",
            default="",
            help="RTSP URI (required for add)",
        )
        parser.add_argument(
            "--models", dest="models", default="", help="Comma-separated model names"
        )

    def handle(self, *args, **options):
        redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
        channel = "deepstream:commands"
        action = options["action"]
        camera_id = options["camera_id"]
        rtsp_uri = options.get("rtsp_uri", "")
        models_str = options.get("models", "")

        models = (
            [m.strip() for m in models_str.split(",") if m.strip()]
            if models_str
            else []
        )

        if action == "add" and not rtsp_uri:
            self.stderr.write(self.style_error("add requires --rtsp-uri"))
            return

        payload = {
            "action": action,
            "camera_id": camera_id,
        }
        if action == "add":
            payload["rtsp_uri"] = rtsp_uri
            payload["models"] = models

        try:
            client = redis.from_url(redis_url, decode_responses=True)
            client.publish(channel, json.dumps(payload))
            self.stdout.write(
                self.style_success(
                    f"Sent {action} command for camera {camera_id} to DeepStream"
                )
            )
        except Exception as e:
            self.stderr.write(self.style_error(f"Failed to send command: {e}"))
