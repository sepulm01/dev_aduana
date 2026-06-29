import logging
import os
import signal
import socket
import struct
import sys
import time
from datetime import datetime

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

logger = logging.getLogger("snapshot_receiver")

END_MARKER = b"END!"
HEADER_FMT = "<II16sffffQI"
HEADER_SIZE = struct.calcsize(HEADER_FMT)


class SnapshotReceiver:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self._running = False
        self._sock = None

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(5)
        self._sock.settimeout(1.0)
        self._running = True
        logger.info("Snapshot receiver listening on %s:%d", self.host, self.port)

        while self._running:
            try:
                conn, addr = self._sock.accept()
                logger.info("Connection from %s:%d", addr[0], addr[1])
                self._handle_client(conn, addr)
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    logger.warning("Accept error: %s", e)

    def stop(self):
        self._running = False
        if self._sock:
            self._sock.close()
            self._sock = None

    def _handle_client(self, conn, addr):
        buf = b""
        try:
            conn.settimeout(10.0)
            while self._running:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk

                while True:
                    end_idx = buf.find(END_MARKER)
                    if end_idx == -1:
                        break

                    jpeg_bytes = buf[:end_idx]
                    remaining = buf[end_idx + len(END_MARKER):]

                    if len(remaining) < HEADER_SIZE:
                        break

                    header_raw = remaining[:HEADER_SIZE]
                    buf = remaining[HEADER_SIZE:]

                    try:
                        pkt = struct.unpack(HEADER_FMT, header_raw)
                    except struct.error:
                        logger.warning("Malformed header from %s", addr)
                        buf = b""
                        break

                    device_id = pkt[0]
                    source_id = pkt[1]
                    snap_type = pkt[2].decode("ascii", errors="replace").strip("\x00")
                    bbox_left = pkt[3]
                    bbox_top = pkt[4]
                    bbox_width = pkt[5]
                    bbox_height = pkt[6]
                    timestamp_ms = pkt[7]
                    jpeg_size = pkt[8]

                    if len(jpeg_bytes) != jpeg_size:
                        logger.warning(
                            "JPEG size mismatch: got %d expected %d device=%d",
                            len(jpeg_bytes), jpeg_size, device_id,
                        )

                    self._process_snapshot(
                        device_id, source_id, snap_type,
                        bbox_left, bbox_top, bbox_width, bbox_height,
                        timestamp_ms / 1000.0, jpeg_bytes,
                    )

        except socket.timeout:
            pass
        except Exception as e:
            logger.warning("Client handler error for %s: %s", addr, e)
        finally:
            conn.close()

    def _process_snapshot(self, device_id, source_id, snap_type,
                          bbox_left, bbox_top, bbox_width, bbox_height,
                          timestamp, jpeg_bytes):
        logger.info(
            "Snapshot: device=%d type=%s size=%d bbox=[%.2f,%.2f,%.2f,%.2f]",
            device_id, snap_type, len(jpeg_bytes),
            bbox_left, bbox_top, bbox_width, bbox_height,
        )

        if snap_type in ("roi", "lc", "oc"):
            self._save_incident_snapshot(device_id, jpeg_bytes)
        elif snap_type == "face":
            self._save_face_crop(device_id, jpeg_bytes, bbox_left, bbox_top,
                                 bbox_width, bbox_height)

    def _save_incident_snapshot(self, device_id, jpeg_bytes):
        try:
            from incidents.models import Incident

            now = datetime.now()
            incident = Incident.objects.filter(
                device_id=device_id,
                status="active",
            ).order_by("-created_at").first()

            if incident and not incident.snapshot:
                ts = now.strftime("%Y%m%d_%H%M%S")
                filename = f"incident_{incident.id}_{ts}_{device_id}.jpg"
                incident.snapshot.save(
                    filename, ContentFile(jpeg_bytes), save=True,
                )
                logger.info(
                    "Saved snapshot for incident #%d device=%d",
                    incident.id, device_id,
                )
            else:
                logger.info(
                    "No active incident for device=%d, skipping snapshot",
                    device_id,
                )
        except Exception as e:
            logger.warning("Failed to save incident snapshot: %s", e)

    def _save_face_crop(self, device_id, jpeg_bytes,
                        bbox_left, bbox_top, bbox_width, bbox_height):
        try:
            from datetime import datetime

            now = datetime.now()
            month_dir = now.strftime("%Y/%m/%d")
            base = os.environ.get("MEDIA_ROOT", "/app/media")
            crop_dir = os.path.join(base, "face_crops", month_dir)
            os.makedirs(crop_dir, exist_ok=True)

            ts = now.strftime("%H%M%S%f")
            filename = f"face_d{device_id}_{ts}.jpg"
            filepath = os.path.join(crop_dir, filename)

            with open(filepath, "wb") as f:
                f.write(jpeg_bytes)

            logger.info(
                "Saved face crop device=%d bbox=[%.2f,%.2f,%.2f,%.2f] -> %s",
                device_id, bbox_left, bbox_top, bbox_width, bbox_height,
                filepath,
            )
        except Exception as e:
            logger.warning("Failed to save face crop: %s", e)


class Command(BaseCommand):
    help = "TCP server receiving GPU-encoded snapshots from DeepStream"

    def handle(self, *args, **options):
        host = os.environ.get("SNAPSHOT_BIND_HOST", "0.0.0.0")
        port = int(os.environ.get("SNAPSHOT_BIND_PORT", "12349"))

        receiver = SnapshotReceiver(host, port)

        def signal_handler(sig):
            logger.info("Received %s, shutting down", sig)
            receiver.stop()
            sys.exit(0)

        try:
            signal.signal(signal.SIGTERM, lambda s, f: signal_handler(s))
            signal.signal(signal.SIGINT, lambda s, f: signal_handler(s))
        except Exception:
            pass

        receiver.start()
