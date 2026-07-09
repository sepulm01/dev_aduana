import logging
import os
import socket
import struct
from datetime import datetime, timedelta, timezone as dt_timezone

import numpy as np
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.utils import timezone as dj_timezone
from PIL import Image
from shapely.geometry import Point, Polygon

logger = logging.getLogger("crop_receiver")

END_MARKER = b"END!"
HEADER_FMT = "<IIIQ5fIQI"
HEADER_SIZE = struct.calcsize(HEADER_FMT)

GAP_THRESHOLD = 3.0
COLOR_THRESHOLD = 0.25
BBOX_JUMP_THRESHOLD = 0.3
GAP_CROSS_SOURCE = 5.0


def _hsv_distance(c1, c2):
    dh = min(abs(c1[0] - c2[0]), 1.0 - abs(c1[0] - c2[0]))
    ds = abs(c1[1] - c2[1])
    dv = abs(c1[2] - c2[2])
    return ((dh * 1.5) ** 2 + ds ** 2 + (dv * 0.5) ** 2) ** 0.5


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


_roi_shapes_cache = {}
_roi_shapes_updated = None


def load_roi_shapes():
    global _roi_shapes_cache, _roi_shapes_updated
    from devices.models import AnalyticsPreset

    _roi_shapes_cache.clear()
    presets = AnalyticsPreset.objects.filter(shapes__isnull=False).exclude(shapes=[])
    for ap in presets:
        polygons = []
        for shape in ap.shapes:
            if shape.get("object") == "polygon" and shape.get("isClosed", True):
                points = shape.get("points", [])
                if len(points) >= 3:
                    name = shape.get("name", "")
                    poly = Polygon([(p["x"], p["y"]) for p in points])
                    polygons.append((name, poly))
        if polygons:
            _roi_shapes_cache[ap.device_id] = polygons
    _roi_shapes_updated = dj_timezone.now()


def crop_roi_name(device_id, source_id, bbox_left, bbox_top, bbox_width, bbox_height):
    global _roi_shapes_cache, _roi_shapes_updated
    if _roi_shapes_updated is None or (dj_timezone.now() - _roi_shapes_updated).total_seconds() > 30:
        load_roi_shapes()
    polygons = _roi_shapes_cache.get(device_id, [])
    if not polygons:
        return ""
    cx = bbox_left + bbox_width / 2
    cy = bbox_top + bbox_height / 2
    pt = Point(cx, cy)
    for name, poly in polygons:
        if poly.contains(pt):
            return name
    return ""


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

            h, s, v = extract_avg_hsv(detection.crop.path)
            if h is not None:
                detection.dominant_color_h = h
                detection.dominant_color_s = s
                detection.dominant_color_v = v

            roi = crop_roi_name(device_id, source_id, bbox_left, bbox_top, bbox_width, bbox_height)
            if roi:
                detection.roi_name = roi

            event = self._find_or_create_event(detection)
            if event:
                detection.event = event

            detection.save()

            if class_id == 3 and not detection.ocr_processed:
                from aduana.tasks import process_ocr
                process_ocr.delay(detection.id)

        except Exception as e:
            logger.error("_process_crop error: %s", e)

    def _find_or_create_event(self, detection):
        from aduana.models import ContainerEvent

        ts = detection.timestamp
        window_start = ts - timedelta(seconds=15)

        event = (
            ContainerEvent.objects
            .filter(seal_status="processing", timestamp_start__gte=window_start)
            .order_by("-timestamp_start")
            .first()
        )

        if not event:
            return ContainerEvent.objects.create(seal_status="processing", timestamp_start=ts)

        recent = list(event.detections.order_by("-timestamp")[:10])
        if not recent:
            return event

        last_det = recent[0]
        gap = (ts - last_det.timestamp).total_seconds()
        cross_source = detection.source_id != last_det.source_id
        threshold = GAP_CROSS_SOURCE if cross_source else GAP_THRESHOLD

        new_color = (detection.dominant_color_h, detection.dominant_color_s, detection.dominant_color_v)

        event_colors = []
        for d in recent:
            if d.dominant_color_h is not None:
                event_colors.append((d.dominant_color_h, d.dominant_color_s, d.dominant_color_v))

        color_diff = None
        if new_color[0] is not None and len(event_colors) >= 2:
            avg_h = sum(c[0] for c in event_colors) / len(event_colors)
            avg_s = sum(c[1] for c in event_colors) / len(event_colors)
            avg_v = sum(c[2] for c in event_colors) / len(event_colors)
            color_diff = _hsv_distance(new_color, (avg_h, avg_s, avg_v))

        new_cx = detection.bbox_left + detection.bbox_width / 2
        new_cy = detection.bbox_top + detection.bbox_height / 2
        last_cx = last_det.bbox_left + last_det.bbox_width / 2
        last_cy = last_det.bbox_top + last_det.bbox_height / 2
        bbox_jump = ((new_cx - last_cx) ** 2 + (new_cy - last_cy) ** 2) ** 0.5

        new_container = False

        if gap > threshold and color_diff is not None and color_diff > COLOR_THRESHOLD:
            new_container = True
        elif gap > threshold and bbox_jump > BBOX_JUMP_THRESHOLD:
            new_container = True
        elif color_diff is not None and color_diff > COLOR_THRESHOLD and gap > 1.0:
            new_container = True

        if new_container:
            return ContainerEvent.objects.create(seal_status="processing", timestamp_start=ts)

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
