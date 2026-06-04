"""
Diagnostic script: draw 2d106det landmarks on detection crops.
Usage: python3 debug_landmarks.py <detection_pk>
Saves result to /var/www/dev_security/videos/landmarks_det_<pk>.jpg
"""
import os, sys, json
import cv2
import numpy as np

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.path.insert(0, "/app")
import django
django.setup()

from detections.models import Detection

REGIONS = [
    ("Contorno", 0, 33, (34, 204, 102)),
    ("Ceja izq", 33, 43, (255, 136, 0)),
    ("Ceja der", 43, 53, (255, 136, 0)),
    ("Nariz", 53, 68, (255, 221, 0)),
    ("Ojo izq", 68, 76, (0, 221, 255)),
    ("Ojo der", 76, 84, (0, 221, 255)),
    ("Boca ext", 84, 96, (238, 68, 255)),
    ("Boca int", 96, 106, (238, 68, 255)),
]


def draw_landmarks(img, landmarks, method="center"):
    """Draw landmarks on image. method='center' uses (x+1)*w/2, method='direct' uses x*w."""
    h, w = img.shape[:2]
    overlay = img.copy()
    for name, start, end, color in REGIONS:
        for i in range(start, end):
            x_lm = landmarks[i * 2]
            y_lm = landmarks[i * 2 + 1]
            if method == "center":
                x = int((x_lm + 1.0) * w / 2)
                y = int((y_lm + 1.0) * h / 2)
            else:
                x = int(x_lm * w)
                y = int(y_lm * h)
            x = max(0, min(w - 1, x))
            y = max(0, min(h - 1, y))
            cv2.circle(overlay, (x, y), 2, color, -1)
    return cv2.addWeighted(overlay, 0.7, img, 0.3, 0)


def main():
    pk = int(sys.argv[1]) if len(sys.argv) > 1 else None
    if pk:
        detections = [Detection.objects.get(pk=pk)]
    else:
        detections = Detection.objects.filter(
            landmarks__isnull=False
        ).order_by("-timestamp")[:3]

    for det in detections:
        if not det.crop or not det.landmarks:
            print(f"Detection #{det.pk}: no crop or landmarks, skipping")
            continue

        img_path = det.crop.path
        if not os.path.exists(img_path):
            print(f"Detection #{det.pk}: file not found at {img_path}")
            continue

        img = cv2.imread(img_path)
        if img is None:
            print(f"Detection #{det.pk}: failed to read image")
            continue

        landmarks = det.landmarks
        if len(landmarks) < 212:
            print(f"Detection #{det.pk}: landmarks too short ({len(landmarks)})")
            continue

        print(f"\nDetection #{det.pk} — {img.shape[1]}x{img.shape[0]} — {len(landmarks)} landmarks")
        print(f"  First 6 landmarks: {landmarks[:12]}")
        print(f"  Range x: [{min(landmarks[::2]):.2f}, {max(landmarks[::2]):.2f}]")
        print(f"  Range y: [{min(landmarks[1::2]):.2f}, {max(landmarks[1::2]):.2f}]")

        img_center = draw_landmarks(img, landmarks, "center")
        img_direct = draw_landmarks(img, landmarks, "direct")

        out_base = f"/var/www/dev_security/videos/landmarks_det_{det.pk}"

        h_center = img_center.shape[0]
        h_direct = img_direct.shape[0]
        img_side = np.hstack([img_center, img_direct]) if h_center == h_direct else img_center

        cv2.putText(
            img_center, "center mapping (x+1)*w/2", (5, 20),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1,
        )
        if h_center == h_direct:
            cv2.putText(
                img_direct, "direct mapping x*w", (5, 20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1,
            )

        out_path = out_base + ".jpg"
        cv2.imwrite(out_path, img_side)
        print(f"  Saved: {out_path}")

        out_center = out_base + "_center.jpg"
        cv2.imwrite(out_center, img_center)
        print(f"  Saved: {out_center}")

        out_direct = out_base + "_direct.jpg"
        cv2.imwrite(out_direct, img_direct)
        print(f"  Saved: {out_direct}")


if __name__ == "__main__":
    main()
