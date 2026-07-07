import logging
import os
import socket
import struct
from datetime import datetime, timezone as dt_timezone

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger("crop_receiver")

END_MARKER = b"END!"
HEADER_FMT = "<IIIQ5fIQI"
HEADER_SIZE = struct.calcsize(HEADER_FMT)


class CropReceiver:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self._running = False
        self._sock = None

    def start(self):
        import django

        django.setup()

        from aduana.models import ContainerDetection
        from devices.models import Device

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(5)
        self._sock.settimeout(1.0)
        self._running = True
        logger.info("Crop receiver listening on %s:%d", self.host, self.port)

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
        from aduana.models import ContainerDetection
        from devices.models import Device

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
                    class_id = pkt[2]
                    object_id = pkt[3]
                    confidence = pkt[4]
                    bbox_left = pkt[5]
                    bbox_top = pkt[6]
                    bbox_width = pkt[7]
                    bbox_height = pkt[8]
                    frame_num = pkt[9]
                    timestamp_ms = pkt[10]
                    jpeg_size = pkt[11]

                    if len(jpeg_bytes) != jpeg_size:
                        logger.warning(
                            "JPEG size mismatch: got %d expected %d device=%d",
                            len(jpeg_bytes), jpeg_size, device_id,
                        )

                    try:
                        self._process_crop(
                            device_id=device_id,
                            source_id=source_id,
                            class_id=class_id,
                            object_id=object_id,
                            confidence=confidence,
                            bbox_left=bbox_left,
                            bbox_top=bbox_top,
                            bbox_width=bbox_width,
                            bbox_height=bbox_height,
                            frame_num=frame_num,
                            timestamp_ms=timestamp_ms,
                            jpeg_bytes=jpeg_bytes,
                        )
                    except Exception as e:
                        logger.error("Error processing crop: %s", e)

        except Exception as e:
            logger.error("Client handler error: %s", e)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _process_crop(self, device_id, source_id, class_id, object_id,
                      confidence, bbox_left, bbox_top, bbox_width, bbox_height,
                      frame_num, timestamp_ms, jpeg_bytes):
        from django.utils import timezone as dj_timezone

        from aduana.models import ContainerDetection, ContainerEvent

        try:
            device = None
            try:
                from devices.models import Device
                device = Device.objects.get(id=device_id)
            except Exception:
                pass

            ts = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=dt_timezone.utc)

            detection = ContainerDetection(
                device=device,
                source_id=source_id,
                class_id=class_id,
                object_id=object_id,
                frame_num=frame_num,
                confidence=confidence,
                bbox_left=bbox_left,
                bbox_top=bbox_top,
                bbox_width=bbox_width,
                bbox_height=bbox_height,
                timestamp=ts,
                ocr_texts=[],
            )

            filename = (
                f"dev{device_id}_src{source_id}_cls{class_id}_"
                f"obj{object_id}_{int(timestamp_ms)}.jpg"
            )
            detection.crop.save(filename, ContentFile(jpeg_bytes), save=False)

            event = self._find_or_create_event(ts, device_id if device else None)
            if event:
                detection.event = event

            detection.save()

            if class_id == 3 and not detection.ocr_processed:
                from aduana.tasks import process_ocr
                process_ocr.delay(detection.id)

        except Exception as e:
            logger.error("_process_crop error: %s", e)

    def _find_or_create_event(self, ts, device_id):
        from aduana.models import ContainerEvent

        window_seconds = 15
        window_start = ts - __import__('datetime').timedelta(seconds=window_seconds)

        event = (
            ContainerEvent.objects
            .filter(
                seal_status="processing",
                timestamp_start__gte=window_start,
            )
            .order_by("-timestamp_start")
            .first()
        )

        if event:
            return event

        event = ContainerEvent.objects.create(
            seal_status="processing",
            timestamp_start=ts,
        )
        return event


class Command(BaseCommand):
    help = "TCP server receiving crops from DeepStream pipeline"

    def handle(self, **options):
        host = os.environ.get("CROP_RECEIVER_HOST", "0.0.0.0")
        port = int(os.environ.get("CROP_RECEIVER_PORT", 12347))
        receiver = CropReceiver(host, port)
        try:
            receiver.start()
        except KeyboardInterrupt:
            receiver.stop()
