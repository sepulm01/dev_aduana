import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time

import redis
from redis import exceptions as redis_exceptions
from channels.layers import InMemoryChannelLayer, get_channel_layer
from asgiref.sync import async_to_sync

from django.core.management.base import BaseCommand

logger = logging.getLogger("redis_event_bridge")


class RedisEventBridge:
    def __init__(self, redis_url):
        self.redis_url = redis_url
        self._running = False
        self._thread = None
        self._preset_cache = {}
        self._preset_thread = None

    def _refresh_presets(self):
        try:
            r = redis.from_url(self.redis_url, decode_responses=True)
            keys = r.keys("device:*:active_preset")
            cache = {}
            for k in keys:
                try:
                    dev_id = int(k.split(":")[1])
                except (IndexError, ValueError):
                    continue
                val = r.get(k)
                if val:
                    cache[dev_id] = val
            self._preset_cache = cache
        except redis_exceptions.ConnectionError:
            pass
        except Exception as e:
            logger.warning("Preset refresh error: %s", e)

    def _preset_refresh_loop(self):
        while self._running:
            self._refresh_presets()
            time.sleep(5)

    def _filter_ivs_event(self, device_id, event_data):
        active_token = self._preset_cache.get(device_id)
        if active_token is None:
            return event_data

        data = event_data.get("data", {})
        objects = data.get("Object", [])
        if objects:
            for obj in objects:
                for key in ("roi", "lc", "oc"):
                    vals = obj.get(key, []) or []
                    obj[key] = [v for v in vals if v.startswith(f"{active_token}_")]
                direction = obj.get("direction", "")
                if direction and not direction.startswith(f"{active_token}_"):
                    obj["direction"] = ""

        analytics = data.get("analytics", {})
        if analytics:
            filtered = {}
            for k, v in analytics.items():
                if k.startswith(f"{active_token}_"):
                    filtered[k] = v
            data["analytics"] = filtered

        return event_data

    def _send_to_channel(self, device_id, event_data):
        try:
            filtered = self._filter_ivs_event(device_id, event_data)
            if filtered is None:
                return
            channel_layer = get_channel_layer()
            if channel_layer is None:
                logger.warning("Channel layer not available")
                return
            group_name = f"device_{device_id}"
            async_to_sync(channel_layer.group_send)(
                group_name,
                {
                    "type": "ivs_event",
                    "device_id": device_id,
                    **filtered,
                },
            )
        except Exception as e:
            logger.warning(
                "Failed to send event to channel for device %s: %s", device_id, e
            )

    def _run_sync(self):
        self._running = True
        while self._running:
            try:
                client = redis.from_url(self.redis_url, decode_responses=True)
                client.ping()
                pubsub = client.pubsub()
                pubsub.psubscribe("device:*:events")
                logger.info("Subscribed to Redis pubsub device:*:events")
                self._running = True

                while self._running:
                    msg = pubsub.get_message(timeout=1.0)
                    if msg and msg["type"] == "pmessage":
                        channel = msg["channel"]
                        try:
                            device_id = int(channel.split(":")[1])
                            event_data = json.loads(msg["data"])
                            self._send_to_channel(device_id, event_data)
                        except (IndexError, ValueError, json.JSONDecodeError) as e:
                            logger.warning("Bad pubsub message on %s: %s", channel, e)
            except redis_exceptions.ConnectionError as e:
                logger.warning("Redis connection error: %s. Retrying in 5s...", e)
                if self._running:
                    time.sleep(5)
            except Exception as e:
                logger.warning("Redis pubsub error: %s. Retrying in 5s...", e)
                if self._running:
                    time.sleep(5)

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run_sync, daemon=True)
        self._thread.start()
        self._preset_thread = threading.Thread(target=self._preset_refresh_loop, daemon=True)
        self._preset_thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        if self._preset_thread:
            self._preset_thread.join(timeout=2)


class Command(BaseCommand):
    help = "Run Redis-to-Channels event bridge for IVS events"

    def handle(self, *args, **options):
        redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
        logger.info("Starting Redis event bridge, connecting to %s", redis_url)
        bridge = RedisEventBridge(redis_url)

        def signal_handler(sig):
            logger.info("Received %s, shutting down", sig)
            bridge.stop()
            sys.exit(0)

        try:
            signal.signal(signal.SIGTERM, lambda s, f: signal_handler(s))
            signal.signal(signal.SIGINT, lambda s, f: signal_handler(s))
        except Exception:
            pass

        bridge.start()
        logger.info("Redis event bridge started")

        while bridge._running:
            try:
                signal.pause()
            except InterruptedError:
                break
            except AttributeError:
                break
