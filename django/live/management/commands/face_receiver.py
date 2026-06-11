import math
import os
import pickle
import socket
import struct
import threading
import time
from datetime import datetime

import cv2
import numpy as np
import onnxruntime
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.conf import settings
from django.utils import timezone
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from pgvector.django import CosineDistance

from detections.models import Detection, IdentityGroup

PACK_FORMAT = "=I Q f 4f Q 10f"
PACK_SIZE = struct.calcsize(PACK_FORMAT)
END_MARKER = b"END!"

# ---- adjustable thresholds ----
# POSE_YAW_THRESHOLD: max degrees of yaw (from 1k3d68 3D pose estimator)
# EMBEDDING_NORM_MIN/MAX: valid embedding L2 norm range for w600k_r50
#   (this model outputs norms ~9-13 due to mean=(1,1,1) preprocessing)
# MATCH_COOLDOWN_SEC: same-person match suppression window (settings override)
# MATCH_DISTANCE_THRESHOLD: cosine distance below which two embeddings are same identity
POSE_YAW_THRESHOLD = 60.0
EMBEDDING_NORM_MIN = 3.0
EMBEDDING_NORM_MAX = 25.0
MATCH_COOLDOWN_SEC = getattr(settings, "FACE_MATCH_COOLDOWN_SECONDS", 10)
MATCH_DISTANCE_THRESHOLD = 0.35

KPS_COUNT = 5

ARC_DST = np.array([
    [38.2946, 51.6963],
    [73.5318, 51.5014],
    [56.0252, 71.7366],
    [41.5493, 92.3655],
    [70.7299, 92.2041],
], dtype=np.float32)


def _get_models_dir():
    for candidate in (
        "/opt/computer_vision/models/retinaface_det10g",
        os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..",
            "computer_vision", "models", "retinaface_det10g",
        ),
    ):
        if os.path.isdir(candidate):
            return candidate
    return "/opt/computer_vision/models/retinaface_det10g"


class PoseEstimator:
    def __init__(self):
        model_path = os.path.join(_get_models_dir(), "1k3d68.onnx")
        meanshape_path = os.path.join(_get_models_dir(), "meanshape_68.pkl")

        self.session = onnxruntime.InferenceSession(
            model_path, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name

        with open(meanshape_path, "rb") as f:
            self.mean_lmk = pickle.load(f)

    def get_yaw(self, jpeg_bytes):
        nparr = np.frombuffer(jpeg_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return None

        img = cv2.resize(img, (192, 192))
        blob = cv2.dnn.blobFromImage(
            img, 1.0 / 128.0, (192, 192), (127.5, 127.5, 127.5), swapRB=True
        )

        pred = self.session.run(None, {self.input_name: blob})[0][0]
        if pred.shape[0] < 68 * 3:
            return None

        pred = pred.reshape((-1, 3))
        pred = pred[-68:, :]
        pred[:, 0:2] += 1.0
        pred[:, 0:2] *= 96.0
        pred[:, 2] *= 96.0

        X_homo = np.hstack([self.mean_lmk, np.ones([68, 1])])
        P = np.linalg.lstsq(X_homo, pred, rcond=None)[0].T
        R1 = P[0:1, :3]
        R2 = P[1:2, :3]
        r1 = R1 / np.linalg.norm(R1)
        r2 = R2 / np.linalg.norm(R2)
        r3 = np.cross(r1, r2)
        R = np.concatenate((r1, r2, r3), 0)
        sy = math.sqrt(R[0, 0] * R[0, 0] + R[1, 0] * R[1, 0])
        if sy < 1e-6:
            return 0.0
        yaw = math.atan2(-R[2, 0], sy)
        return float(math.degrees(yaw))


class FaceAligner:
    def __init__(self):
        model_path = os.path.join(_get_models_dir(), "w600k_r50.onnx")
        self.session = onnxruntime.InferenceSession(
            model_path, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name

    def align_and_embed(self, jpeg_bytes, kps):
        nparr = np.frombuffer(jpeg_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return None

        src = kps.reshape((5, 2)).astype(np.float32)

        mean_s = src.mean(axis=0)
        mean_d = ARC_DST.mean(axis=0)
        src_c = src - mean_s
        dst_c = ARC_DST - mean_d

        std_s = np.std(src_c)
        std_d = np.std(dst_c)
        if std_s < 1e-8 or std_d < 1e-8:
            return None
        src_n = src_c / std_s
        dst_n = dst_c / std_d

        a = b = 0.0
        for i in range(5):
            a += src_n[i, 0] * dst_n[i, 0] + src_n[i, 1] * dst_n[i, 1]
            b += src_n[i, 0] * dst_n[i, 1] - src_n[i, 1] * dst_n[i, 0]

        M = np.array([
            [a, -b, mean_d[0] - a * mean_s[0] + b * mean_s[1]],
            [b,  a, mean_d[1] - b * mean_s[0] - a * mean_s[1]],
        ], dtype=np.float32)

        aligned = cv2.warpAffine(img, M, (112, 112), borderValue=0.0)
        aligned_rgb = cv2.cvtColor(aligned, cv2.COLOR_BGR2RGB)
        blob = cv2.dnn.blobFromImage(
            aligned_rgb, 1.0 / 127.5, (112, 112), (1.0, 1.0, 1.0), swapRB=False
        )

        pred = self.session.run(None, {self.input_name: blob})[0][0]
        return pred.astype(np.float32).tolist()


_pose_estimator = None
_face_aligner = None


def get_pose_estimator():
    global _pose_estimator
    if _pose_estimator is None:
        _pose_estimator = PoseEstimator()
    return _pose_estimator


def get_face_aligner():
    global _face_aligner
    if _face_aligner is None:
        _face_aligner = FaceAligner()
    return _face_aligner


class Command(BaseCommand):
    help = "TCP socket server that receives face crops from DeepStream"

    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.last_area = {}
        self._frame_count = 0
        self._yaw_skip_count = 0
        self._obj_area_ttl = 300
        self._sweep_every_n = 100

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
                    if len(after) < PACK_SIZE:
                        break
                    self._process_frame(jpeg_bytes, after[:PACK_SIZE])
                    stream = after[PACK_SIZE:]
        finally:
            try:
                client_socket.close()
            except OSError:
                pass

    def _sweep_stale_objects(self, now):
        cutoff = now - self._obj_area_ttl
        stale = [k for k, (_, ts) in self.last_area.items() if ts < cutoff]
        for k in stale:
            del self.last_area[k]

    def _process_frame(self, jpeg_bytes, packed_data):
        vals = struct.unpack(PACK_FORMAT, packed_data)
        device_id = vals[0]
        object_id = vals[1]
        left = vals[3]
        top = vals[4]
        width = vals[5]
        height = vals[6]
        timestamp_ms = vals[7]
        kps = np.array(vals[8:18], dtype=np.float32)

        area = width * height
        key = (device_id, object_id)

        with self.lock:
            self._frame_count += 1
            if self._frame_count % self._sweep_every_n == 0:
                self._sweep_stale_objects(time.monotonic())

            prev = self.last_area.get(key)
            if prev is not None and area <= prev[0]:
                return
            self.last_area[key] = (area, time.monotonic())

        yaw = get_pose_estimator().get_yaw(jpeg_bytes)
        if yaw is not None and abs(yaw) > POSE_YAW_THRESHOLD:
            self._yaw_skip_count += 1
            if self._yaw_skip_count % 100 == 0:
                print(f"[FaceReceiver] yaw skip x{self._yaw_skip_count} "
                      f"(last yaw={yaw:.1f} dev={device_id} obj={object_id})")
            return

        embedding = get_face_aligner().align_and_embed(jpeg_bytes, kps)
        if embedding is None:
            return

        emb_norm = float(np.linalg.norm(embedding))
        if emb_norm < EMBEDDING_NORM_MIN or emb_norm > EMBEDDING_NORM_MAX:
            return

        self._save_detection(
            device_id, object_id, timestamp_ms,
            left, top, width, height,
            jpeg_bytes, kps, embedding,
        )

    def _save_detection(self, device_id, object_id, timestamp_ms,
                        left, top, width, height,
                        jpeg_bytes, kps, embedding):
        identity_group = None
        result = self._check_match(device_id, embedding)
        if result is not None:
            matched, distance = result
            now = timezone.now()
            delta = (now - matched.timestamp).total_seconds()
            if 0 < delta < MATCH_COOLDOWN_SEC:
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
                datetime.fromtimestamp(timestamp_ms / 1000.0)
            )
            identity_group.detection_count += 1
            identity_group.save(update_fields=["detection_count", "last_seen"])
            print(f"[FaceReceiver] Re-ID dev={device_id} obj={object_id} "
                  f"-> matched={matched.object_id} dist={distance:.4f}")
            self._broadcast_match(device_id, object_id, matched, distance)
        else:
            ts = timezone.make_aware(
                datetime.fromtimestamp(timestamp_ms / 1000.0)
            )
            identity_group = IdentityGroup.objects.create(
                first_seen=ts, last_seen=ts,
            )
            print(f"[FaceReceiver] New dev={device_id} obj={object_id}")
            self._broadcast_new_face(device_id, object_id, ts)

        obj = Detection(
            device_id=device_id,
            object_id=object_id,
            class_label="face",
            identity_group=identity_group,
            bbox_left=left,
            bbox_top=top,
            bbox_width=width,
            bbox_height=height,
            quality_score=1.0,
            embedding=embedding,
            landmarks=kps.tolist(),
            timestamp=datetime.fromtimestamp(timestamp_ms / 1000.0),
        )

        if jpeg_bytes:
            filename = f"{device_id}_{object_id}_{int(timestamp_ms)}.jpg"
            obj.crop.save(filename, ContentFile(jpeg_bytes), save=False)

        obj.save()

    def _check_match(self, device_id, embedding):
        try:
            match = (
                Detection.objects.filter(device_id=device_id)
                .exclude(embedding__isnull=True)
                .annotate(distance=CosineDistance("embedding", embedding))
                .filter(distance__lt=MATCH_DISTANCE_THRESHOLD)
                .order_by("distance")
                .first()
            )
            if match:
                return (match, float(match.distance))
            return None
        except Exception as e:
            print(f"[FaceReceiver] Match error: {e}")
            return None

    def _broadcast_match(self, device_id, object_id, matched, distance):
        try:
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                f"device_{device_id}",
                {
                    "type": "face_match",
                    "device_id": device_id,
                    "object_id": object_id,
                    "matched_id": matched.object_id,
                    "distance": round(distance, 4),
                    "timestamp": str(timezone.now()),
                },
            )
        except Exception as e:
            print(f"[FaceReceiver] Broadcast error: {e}")

    def _broadcast_new_face(self, device_id, object_id, ts):
        try:
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                f"device_{device_id}",
                {
                    "type": "new_face",
                    "device_id": device_id,
                    "object_id": object_id,
                    "timestamp": str(ts),
                },
            )
        except Exception as e:
            print(f"[FaceReceiver] Broadcast error: {e}")
