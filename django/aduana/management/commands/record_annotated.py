#!/usr/bin/env python3
"""
Graba video anotado a partir de snapshots ONVIF + detecciones de DeepStream.
Ejecutar dentro del contenedor celery-worker o django-http.
"""
import os, sys, time, json, socket
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
sys.path.insert(0, '/app')
import django
django.setup()

from devices.models import Device
from aduana.models import ContainerDetection
from onvif import ONVIFCamera
from PIL import Image, ImageDraw, ImageFont
import requests
from datetime import datetime, timezone as dt_timezone
import argparse
from io import BytesIO

CLASS_COLORS = {
    0: (0, 255, 0),      # con_sello: green
    1: (255, 0, 0),      # sin_sello: red
    2: (255, 255, 0),    # cont data: yellow
    3: (0, 255, 255),    # container cod: cyan
    4: (128, 128, 128),  # truck: gray
}
CLASS_NAMES = {0: 'con_sello', 1: 'sin_sello', 2: 'cont data', 3: 'container cod', 4: 'truck'}

def capture_snapshot(device, wsdl_dir):
    cam = ONVIFCamera(device.host, device.port, device.username, device.password, wsdl_dir=wsdl_dir)
    media = cam.create_media_service()
    uri = media.GetSnapshotUri({'ProfileToken': device.default_profile_token})
    resp = requests.get(uri.Uri, auth=(device.username, device.password), timeout=10)
    return Image.open(BytesIO(resp.content)).convert('RGB')

def draw_detections(img, detections, source_id):
    draw = ImageDraw.Draw(img)
    w, h = img.size
    for d in detections:
        if d.source_id != source_id:
            continue
        x1 = int(d.bbox_left * w)
        y1 = int(d.bbox_top * h)
        x2 = int((d.bbox_left + d.bbox_width) * w)
        y2 = int((d.bbox_top + d.bbox_height) * h)
        color = CLASS_COLORS.get(d.class_id, (255, 255, 255))
        label = CLASS_NAMES.get(d.class_id, '?')
        draw.rectangle([x1, y1, x2, y2], outline=color, width=3)
        text = f"{label} {d.confidence:.2f}"
        if d.ocr_text:
            text += f" [{d.ocr_text}]"
        draw.text((x1, y1 - 15), text, fill=color)
    return img

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--duration', type=int, default=30, help='Recording duration in seconds')
    parser.add_argument('--output', default='/tmp/annotated.mp4', help='Output file')
    parser.add_argument('--fps', type=int, default=5, help='Frames per second')
    args = parser.parse_args()

    socket.setdefaulttimeout(15)
    WSDL = '/usr/local/lib/python3.12/site-packages/wsdl/'

    devices = list(Device.objects.filter(is_online=True).order_by('id'))
    if not devices:
        print("No online devices")
        return

    print(f"Recording {args.duration}s at {args.fps} FPS for {len(devices)} cameras...")
    start = time.time()
    frames = {d.id: [] for d in devices}

    while time.time() - start < args.duration:
        t0 = time.time()
        recent = list(ContainerDetection.objects.filter(
            timestamp__gte=timezone.now() - timezone.timedelta(seconds=3)
        ).order_by('-timestamp')[:100])

        for dev in devices:
            try:
                img = capture_snapshot(dev, WSDL)
                img = draw_detections(img, recent, dev.id - 1)  # dev 1->src 0, dev 2->src 1
                frames[dev.id].append(img)
                print(f"  [{dev.name}] {len(frames[dev.id])} frames", end='\r')
            except Exception as e:
                print(f"  [{dev.name}] snapshot error: {e}")

        elapsed = time.time() - t0
        sleep_time = max(0, 1.0 / args.fps - elapsed)
        time.sleep(sleep_time)

    # Save as MP4
    for dev in devices:
        output = args.output.replace('.mp4', f'_cam{dev.id}.mp4')
        imgs = frames[dev.id]
        if imgs:
            imgs[0].save(output, save_all=True, append_images=imgs[1:],
                        duration=int(1000/args.fps), loop=0)
            print(f"\nSaved {len(imgs)} frames to {output}")
        else:
            print(f"\nNo frames for {dev.name}")

if __name__ == '__main__':
    main()
