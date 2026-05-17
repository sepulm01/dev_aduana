import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone

import psycopg2
import redis
import requests
from requests.auth import HTTPDigestAuth

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("event-stream-service")

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
DATABASE_URL = os.environ.get("DATABASE_URL")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
CAMERA_TIMEOUT = int(os.environ.get("CAMERA_TIMEOUT", "65"))


def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


def get_devices():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, host, port, username, password, manufacturer,
                       event_listener_enabled
                FROM devices_device
                WHERE event_listener_enabled = true
                  AND username != ''
                  AND password != ''
                """
            )
            rows = cur.fetchall()
            return [
                {
                    "id": row[0],
                    "host": row[1],
                    "port": row[2],
                    "username": row[3],
                    "password": row[4],
                    "manufacturer": row[5] or "",
                    "event_listener_enabled": row[6],
                }
                for row in rows
            ]
    finally:
        conn.close()


def store_event(device_id, code, action, index, data, timestamp):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO devices_deviceevent
                (device_id, code, action, index, data, timestamp)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (device_id, code, action, index, json.dumps(data), timestamp),
            )
        conn.commit()
    finally:
        conn.close()


class DahuaEventWatcher:
    def __init__(self, device, redis_client):
        self.device = device
        self.redis_client = redis_client
        self._session = requests.Session()
        self._running = False
        self._cancel_event = None

    @property
    def base_url(self):
        return f"http://{self.device['host']}:{self.device['port']}"

    @property
    def auth(self):
        return HTTPDigestAuth(self.device["username"], self.device["password"])

    def publish(self, channel, data):
        try:
            self.redis_client.publish(channel, json.dumps(data))
        except Exception as e:
            logger.warning(
                "Redis publish error for device %s: %e", self.device["id"], e
            )

    def parse_event(self, text):
        import re

        code_m = re.search(r"Code=([^;]+)", text)
        action_m = re.search(r";action=([^;]+)", text)
        index_m = re.search(r";index=(\d+)", text)
        data_m = re.search(r";data=(\{.*\})", text)
        if not code_m:
            return None
        code = code_m.group(1).strip()
        action = action_m.group(1).strip() if action_m else "Unknown"
        index = int(index_m.group(1)) if index_m else 0
        data = {}
        if data_m:
            try:
                data = json.loads(data_m.group(1))
            except json.JSONDecodeError:
                pass
        return {
            "code": code,
            "action": action,
            "index": index,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def run(self):
        device_id = self.device["id"]
        channel = f"device:{device_id}:events"
        reconnect_delay = 5

        while self._running:
            try:
                url = f"{self.base_url}/cgi-bin/eventManager.cgi"
                params = {"action": "attach", "codes": ["All"], "heartbeat": "30"}
                resp = self._session.get(
                    url,
                    params=params,
                    auth=self.auth,
                    timeout=CAMERA_TIMEOUT,
                    stream=True,
                )
                resp.raise_for_status()
                logger.info(
                    "Connected to device %s (%s)", device_id, self.device["host"]
                )
                reconnect_delay = 5

                boundary = None
                buffer = b""
                async for chunk in self._async_iter_content(resp):
                    if not self._running:
                        break
                    buffer += chunk
                    if boundary is None:
                        m = re.search(rb"boundary=(\S+)", chunk)
                        if m:
                            boundary = b"--" + m.group(1)
                    if boundary:
                        while True:
                            pos = buffer.find(boundary)
                            if pos == -1:
                                if len(buffer) > 10000:
                                    buffer = buffer[-4096:]
                                break
                            segment = buffer[:pos]
                            buffer = buffer[pos + len(boundary) :]
                            if not segment or segment == b"--":
                                continue
                            body = segment.lstrip(b"\r\n--").strip()
                            if body:
                                try:
                                    text = body.decode("utf-8", errors="ignore")
                                    event = self.parse_event(text)
                                    if event:
                                        self.publish(channel, event)
                                        store_event(
                                            device_id,
                                            event["code"],
                                            event["action"],
                                            event["index"],
                                            event["data"],
                                            event["timestamp"],
                                        )
                                except Exception:
                                    pass
            except requests.RequestException as e:
                logger.warning(
                    "Device %s stream error: %s. Reconnecting in %ds",
                    device_id,
                    e,
                    reconnect_delay,
                )
            except Exception as e:
                logger.warning("Device %s unexpected error: %s", device_id, e)

            if self._running:
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 120)

    async def _async_iter_content(self, resp):
        loop = asyncio.get_event_loop()
        for chunk in resp.iter_content(chunk_size=4096):
            if not self._running:
                break
            await asyncio.sleep(0)
            yield chunk

    def start(self):
        self._running = True
        return self

    def stop(self):
        self._running = False


async def monitor_device(device, redis_client):
    watcher = DahuaEventWatcher(device, redis_client)
    watcher.start()
    try:
        await watcher.run()
    finally:
        watcher.stop()


async def main():
    logger.info("Event stream service starting...")

    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()
    logger.info("Connected to Redis at %s", REDIS_URL)

    tasks = []

    async def refresh_loop():
        while True:
            devices = get_devices()
            logger.info("Polling devices: %d with event_listener_enabled", len(devices))

            running_ids = {t.get_name() for t in tasks}
            desired_ids = {str(d["id"]) for d in devices}

            for task in tasks:
                if task.get_name() not in desired_ids:
                    task.cancel()
                    logger.info("Stopped watcher for device %s", task.get_name())

            for device in devices:
                did = str(device["id"])
                if did not in running_ids:
                    t = asyncio.create_task(
                        monitor_device(device, redis_client),
                        name=did,
                    )
                    tasks.append(t)
                    logger.info("Started watcher for device %s", did)

            await asyncio.sleep(POLL_INTERVAL)

    refresh_task = asyncio.create_task(refresh_loop())

    def signal_handler(sig):
        logger.info("Received signal %s, shutting down...", sig)
        refresh_task.cancel()
        for t in tasks:
            t.cancel()
        loop.stop()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: signal_handler(s))

    await refresh_task


if __name__ == "__main__":
    asyncio.run(main())
