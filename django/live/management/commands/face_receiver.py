import os
import socket
import struct
import threading
import time
from collections import OrderedDict
from datetime import datetime

import cv2
import numpy as np
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.conf import settings
from django.utils import timezone
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from pgvector.django import CosineDistance

from detections.models import Detection, IdentityGroup


PACK_FORMAT = "=I Q f 4f Q"
PACK_SIZE = struct.calcsize(PACK_FORMAT)
EMBEDDING_SIZE = 512 * 4
LANDMARKS_SIZE = 212 * 4
END_MARKER = b"END!"

BUFFER_TIMEOUT_SEC = 3
SCORE_THRESHOLD = 0.0
COOLDOWN_SEC = getattr(settings, "FACE_MATCH_COOLDOWN_SECONDS", 300)
FACE_QUALITY_MIN = getattr(settings, "FACE_QUALITY_MIN_SCORE", 1500)


class FaceBuffer:
    def __init__(self):
        self.buffer = OrderedDict()

    def add(
        self,
        device_id,
        object_id,
        quality_score,
        bbox_left,
        bbox_top,
        bbox_width,
        bbox_height,
        timestamp_ms,
        jpeg_bytes,
        embedding_raw,
        landmarks_raw,
    ):
        area = bbox_width * bbox_height
        if area <= 0:
            area = 0.001
        score = quality_score * (1.0 + area * 0.1)

        if score < SCORE_THRESHOLD:
            return

        key = (device_id, object_id)
        if key in self.buffer:
            existing = self.buffer[key]
            if score <= existing["score"]:
                return
            self.buffer.move_to_end(key)

        self.buffer[key] = {
            "device_id": device_id,
            "object_id": object_id,
            "quality_score": quality_score,
            "bbox": {
                "left": bbox_left,
                "top": bbox_top,
                "width": bbox_width,
                "height": bbox_height,
            },
            "timestamp_ms": timestamp_ms,
            "jpeg_bytes": jpeg_bytes,
            "embedding_raw": embedding_raw,
            "landmarks_raw": landmarks_raw,
            "score": score,
            "last_seen": time.time(),
        }

    def process_timeouts(self, current_time):
        to_flush = []
        for key, entry in list(self.buffer.items()):
            age = current_time - entry["last_seen"]
            if age > BUFFER_TIMEOUT_SEC:
                to_flush.append(key)
        return to_flush

    def flush(self, key):
        return self.buffer.pop(key, None)


class Command(BaseCommand):
    help = "TCP socket server that receives face crops from DeepStream"

    def handle(self, *args, **options):
        host = "0.0.0.0"
        port = 12348
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((host, port))
        server_socket.listen(5)
        print(f"Face receiver listening on {host}:{port}")

        while True:
            client_socket, addr = server_socket.accept()
            print(f"DeepStream connected from {addr}")
            t = threading.Thread(
                target=self._handle_client,
                args=(client_socket, addr),
                daemon=True,
            )
            t.start()

    def _handle_client(self, client_socket, addr):
        buf = FaceBuffer()
        stream = b""
        try:
            while True:
                try:
                    r = client_socket.recv(65536)
                except (ConnectionResetError, OSError):
                    break
                if not r:
                    break
                stream += r

                while True:
                    pos = stream.find(END_MARKER)
                    if pos == -1:
                        break
                    jpeg_bytes = stream[:pos]
                    after = stream[pos + len(END_MARKER):]
                    total_remain = PACK_SIZE + EMBEDDING_SIZE + LANDMARKS_SIZE
                    if len(after) < total_remain:
                        break
                    self._process_packet(buf, jpeg_bytes, after)
                    stream = after[total_remain:]

        except (ConnectionError, OSError, struct.error):
            pass
        finally:
            for key in list(buf.buffer.keys()):
                entry = buf.flush(key)
                if entry:
                    self._save_detection(entry)
            try:
                client_socket.close()
            except OSError:
                pass

    def _recv_exact(self, sock, size):
        data = b""
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise ConnectionError("socket closed")
            data += chunk
        return data

    def _process_packet(self, buf, jpeg_bytes, rest):
        total_expected = PACK_SIZE + EMBEDDING_SIZE + LANDMARKS_SIZE
        if len(rest) < total_expected:
            return

        packed_data = rest[:PACK_SIZE]
        embedding_raw = rest[PACK_SIZE : PACK_SIZE + EMBEDDING_SIZE]
        landmarks_raw = rest[
            PACK_SIZE + EMBEDDING_SIZE : PACK_SIZE + EMBEDDING_SIZE + LANDMARKS_SIZE
        ]

        vals = struct.unpack(PACK_FORMAT, packed_data)
        device_id = vals[0]
        object_id = vals[1]
        quality_score = vals[2]
        left, top, width, height = vals[3], vals[4], vals[5], vals[6]
        timestamp_ms = vals[7]

        buf.add(
            device_id,
            object_id,
            quality_score,
            left,
            top,
            width,
            height,
            timestamp_ms,
            jpeg_bytes,
            embedding_raw,
            landmarks_raw,
        )

        now = time.time()

        timed_out = buf.process_timeouts(now)
        for key in timed_out:
            entry = buf.flush(key)
            if entry:
                self._save_detection(entry)

    def _save_detection(self, entry):
        if entry is None:
            return

        if entry.get("jpeg_bytes"):
            nparr = np.frombuffer(entry["jpeg_bytes"], np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)
            if img is not None and img.size > 0:
                lap_var = cv2.Laplacian(img, cv2.CV_64F).var()
                h, w = img.shape
                quality_score = min(w, h) * lap_var
                if quality_score < FACE_QUALITY_MIN:
                    return

        embedding_list = None
        if len(entry["embedding_raw"]) >= EMBEDDING_SIZE:
            try:
                embedding_list = list(struct.unpack(f"{512}f", entry["embedding_raw"]))
            except struct.error:
                pass

        landmarks_list = None
        if len(entry["landmarks_raw"]) >= LANDMARKS_SIZE:
            try:
                landmarks_list = list(struct.unpack(f"{212}f", entry["landmarks_raw"]))
            except struct.error:
                pass

        if landmarks_list:
            is_lm_zero = all(abs(v) < 1e-10 for v in landmarks_list)
            if is_lm_zero:
                landmarks_list = None

        result = None
        if embedding_list:
            is_zero = all(abs(v) < 1e-10 for v in embedding_list)
            if is_zero:
                embedding_list = None

        identity_group = None
        if embedding_list:
            result = self._check_match(entry["device_id"], embedding_list)
            if result is not None:
                matched, distance = result
                now = timezone.now()
                delta = (now - matched.timestamp).total_seconds()
                if 0 < delta < COOLDOWN_SEC:
                    return
                identity_group = matched.identity_group
                if identity_group is None:
                    identity_group = IdentityGroup.objects.create(
                        first_seen=matched.timestamp,
                        last_seen=matched.timestamp,
                    )
                    matched.identity_group = identity_group
                    matched.save(update_fields=["identity_group"])
                identity_group.last_seen = timezone.make_aware(
                    datetime.fromtimestamp(entry["timestamp_ms"] / 1000.0)
                )
                identity_group.detection_count += 1
                identity_group.save(
                    update_fields=["detection_count", "last_seen"]
                )
            else:
                ts = timezone.make_aware(
                    datetime.fromtimestamp(entry["timestamp_ms"] / 1000.0)
                )
                identity_group = IdentityGroup.objects.create(
                    first_seen=ts,
                    last_seen=ts,
                )

        obj = Detection(
            device_id=entry["device_id"],
            object_id=entry["object_id"],
            class_label="face",
            identity_group=identity_group,
            bbox_left=entry["bbox"]["left"],
            bbox_top=entry["bbox"]["top"],
            bbox_width=entry["bbox"]["width"],
            bbox_height=entry["bbox"]["height"],
            quality_score=entry["quality_score"],
            embedding=embedding_list,
            landmarks=landmarks_list,
            timestamp=datetime.fromtimestamp(entry["timestamp_ms"] / 1000.0),
        )

        if entry["jpeg_bytes"]:
            filename = f"{entry['device_id']}_{entry['object_id']}_{int(entry['timestamp_ms'])}.jpg"
            obj.crop.save(filename, ContentFile(entry["jpeg_bytes"]), save=False)

        obj.save()

        if result is not None:
            self._broadcast_match(obj, matched, distance)
        else:
            self._broadcast_new_face(obj)

        tag = "[FaceReceiver]"
        if result is not None:
            print(
                f"{tag} Saved face (re-id) oid={entry['object_id']} "
                f"score={entry['quality_score']:.2f} -> "
                f"matched={matched.object_id} dist={distance:.4f}"
            )
        else:
            print(
                f"{tag} Saved face (new) oid={entry['object_id']} "
                f"score={entry['quality_score']:.2f}"
            )

    def _check_match(self, device_id, embedding_list):
        if not embedding_list:
            return None
        try:
            match = (
                Detection.objects.filter(device_id=device_id)
                .exclude(embedding__isnull=True)
                .annotate(distance=CosineDistance("embedding", embedding_list))
                .filter(distance__lt=0.35)
                .order_by("distance")
                .first()
            )
            if match:
                return (match, float(match.distance))
            return None
        except Exception as e:
            print(f"[FaceReceiver] Match error: {e}")
            return None

    def _broadcast_match(self, detection, matched, distance):
        try:
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                f"device_{detection.device_id}",
                {
                    "type": "face_match",
                    "device_id": detection.device_id,
                    "object_id": detection.object_id,
                    "matched_id": matched.object_id,
                    "distance": round(distance, 4),
                    "timestamp": str(detection.timestamp),
                },
            )
        except Exception as e:
            print(f"[FaceReceiver] Broadcast error: {e}")

    def _broadcast_new_face(self, detection):
        try:
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                f"device_{detection.device_id}",
                {
                    "type": "new_face",
                    "device_id": detection.device_id,
                    "object_id": detection.object_id,
                    "timestamp": str(detection.timestamp),
                },
            )
        except Exception as e:
            print(f"[FaceReceiver] Broadcast error: {e}")
