import logging
import os
import socket
import struct
from datetime import datetime, timezone as dt_timezone

import numpy as np
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from PIL import Image

logger = logging.getLogger("crop_receiver")

END_MARKER = b"END!"
HEADER_FMT = "<IIIQ5fIQIQ"
HEADER_SIZE = struct.calcsize(HEADER_FMT)

def extract_avg_hsv(image_path):
    try:
        img = Image.open(image_path).convert("RGB")
        arr = np.array(img, dtype=np.float32) / 255.0
        r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
        max_c = np.max(arr, axis=2)
        min_c = np.min(arr, axis=2)
        delta = max_c - min_c
        v = max_c

        mask = (v > 0.15) & (v < 0.95) & (delta > 0.02)
        if mask.sum() < 100:
            return None, None, None

        h = np.zeros_like(max_c)
        h[(mask) & (max_c == r)] = (
            60 * ((g[(mask) & (max_c == r)] - b[(mask) & (max_c == r)]) / delta[(mask) & (max_c == r)])
        ) % 360 / 360.0
        h[(mask) & (max_c == g)] = (
            60 * ((b[(mask) & (max_c == g)] - r[(mask) & (max_c == g)]) / delta[(mask) & (max_c == g)]) + 120
        ) / 360.0
        h[(mask) & (max_c == b)] = (
            60 * ((r[(mask) & (max_c == b)] - g[(mask) & (max_c == b)]) / delta[(mask) & (max_c == b)]) + 240
        ) / 360.0

        s = np.where(max_c > 0.01, delta / max_c, 0)
        return float(np.mean(h[mask])), float(np.mean(s[mask])), float(np.mean(v[mask]))
    except Exception:
        return None, None, None




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
                    truck_id = pkt[12]

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
                            truck_id=truck_id,
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

    def _process_crop(self, device_id, source_id, class_id, object_id, truck_id,
                      confidence, bbox_left, bbox_top, bbox_width, bbox_height,
                      frame_num, timestamp_ms, jpeg_bytes):
        """Raw ingest: store every crop/snapshot with event_id=NULL and take
        NO online decision. Events are built offline by the sweeper task
        (process_raw_detections), which sees the complete time window."""
        from django.utils import timezone as dj_timezone

        from aduana.models import ContainerDetection

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
                truck_id=truck_id,
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

            # Full-frame snapshots (cls 99) are stored as raw detections too;
            # the sweeper assigns the best one to the event's frame_src{sid}.
            if class_id != 99:
                h, s, v = extract_avg_hsv(detection.crop.path)
                if h is not None:
                    detection.dominant_color_h = h
                    detection.dominant_color_s = s
                    detection.dominant_color_v = v

            detection.save()

        except Exception as e:
            logger.error("_process_crop error: %s", e)



class Command(BaseCommand):
    help = "TCP server receiving crops from DeepStream pipeline"

    def handle(self, **options):
        host = os.environ.get("CROP_RECEIVER_HOST", "0.0.0.0")
        port = int(os.environ.get("CROP_RECEIVER_PORT", 12347))
        try:
            from aduana.tasks import check_ocr_vl_health
            if not check_ocr_vl_health():
                self.stderr.write(self.style.ERROR(
                    "*** ALARMA: OCR-VL no responde. El OCR NO funcionara. ***"
                ))
        except ImportError:
            self.stderr.write(self.style.WARNING(
                "check_ocr_vl_health no disponible, omitiendo health check"
            ))
        receiver = CropReceiver(host, port)
        try:
            receiver.start()
        except KeyboardInterrupt:
            receiver.stop()
