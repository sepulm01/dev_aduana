#!/usr/bin/env python3
"""
Graba ambas camaras simultaneamente en segmentos.
Ejecutar en el servidor remoto:
  python3 manage.py record_videos --segments 4 --duration 900
  (4 segmentos de 15 min = 1 hora total)
"""
import os, sys, subprocess, time, signal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.path.insert(0, "/app")
import django

django.setup()

from datetime import datetime
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Record both RTSP cameras simultaneously in fixed-duration segments"

    def add_arguments(self, parser):
        parser.add_argument("--duration", type=int, default=900, help="Duration per segment in seconds (default: 900 = 15 min)")
        parser.add_argument("--segments", type=int, default=4, help="Number of segments (default: 4)")
        parser.add_argument("--output", type=str, default="/opt/computer_vision/record", help="Output directory")
        parser.add_argument("--cameras", type=str, nargs="+", help="RTSP URLs (manual override)")

    def handle(self, **options):
        duration = options["duration"]
        segments = options["segments"]
        outdir = options["output"]
        manual_cams = options.get("cameras")

        os.makedirs(outdir, exist_ok=True)

        if manual_cams:
            rtsp_urls = manual_cams
        else:
            rtsp_urls = self._get_rtsp_urls()

        if len(rtsp_urls) < 2:
            self.stderr.write(f"Need 2 RTSP URLs, got {len(rtsp_urls)}")
            sys.exit(1)

        self.stdout.write(f"Recording {segments} segments x {duration}s = {segments * duration / 60:.0f} min total")
        self.stdout.write(f"Output: {outdir}")
        self.stdout.write(f"Camera 1: {rtsp_urls[0][:60]}...")
        self.stdout.write(f"Camera 2: {rtsp_urls[1][:60]}...")

        total_secs = segments * duration
        for seg in range(1, segments + 1):
            self.stdout.write(f"\n--- Segment {seg}/{segments} ---")
            segment_start = datetime.now()
            label = segment_start.strftime("%Y%m%d_%H%M%S")

            out1 = os.path.join(outdir, f"cam1_seg{seg}_{label}.mp4")
            out2 = os.path.join(outdir, f"cam2_seg{seg}_{label}.mp4")

            procs = []
            for idx, (url, out) in enumerate([(rtsp_urls[0], out1), (rtsp_urls[1], out2)]):
                cmd = [
                    "ffmpeg", "-y",
                    "-rtsp_transport", "tcp",
                    "-i", url,
                    "-t", str(duration),
                    "-c:v", "copy",
                    "-c:a", "aac",
                    "-b:a", "128k",
                    out,
                ]
                self.stdout.write(f"  Cam {idx+1}: {' '.join(cmd[:-1])} ...{os.path.basename(out)}")
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                procs.append(proc)

            for idx, proc in enumerate(procs):
                _, stderr = proc.communicate()
                if proc.returncode != 0:
                    err = stderr.decode(errors="replace")[-300:]
                    self.stderr.write(f"  Cam {idx+1} FAILED (rc={proc.returncode}): {err}")
                else:
                    fsize = os.path.getsize(out1 if idx == 0 else out2) / 1024 / 1024
                    self.stdout.write(f"  Cam {idx+1} done: {fsize:.1f} MB")

            elapsed = (datetime.now() - segment_start).total_seconds()
            self.stdout.write(f"  Segment time: {elapsed:.0f}s")

        self.stdout.write("\nDone.")

    def _get_rtsp_urls(self):
        from devices.models import Device

        devices = list(Device.objects.filter(is_online=True, source_type="rtsp").order_by("host"))
        urls = []
        for d in devices:
            token = d.default_profile_token
            if token and d.stream_uris and token in d.stream_uris:
                uri = d.stream_uris[token]
                parsed = uri.split("://", 1)
                creds = f"{d.username}:{d.password}"
                url = f"{parsed[0]}://{creds}@{parsed[1]}"
                urls.append(url)
        return urls
