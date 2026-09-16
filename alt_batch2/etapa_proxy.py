#!/usr/bin/env python3
"""Etapa proxy: copia liviana 540p del video + calibracion del desfase de
cuadros proxy->original. Guarda fps/total/offset en la BD del video."""
import argparse
import json
import os
import sqlite3
import subprocess

import cv2
import numpy as np

import estado


def calibrar_offset(proxy_path, video_path):
    cal = proxy_path + ".cal.json"
    if os.path.exists(cal):
        try:
            return int(json.load(open(cal))["delta"])
        except Exception:
            pass
    cap_p = cv2.VideoCapture(proxy_path)
    cap_o = cv2.VideoCapture(video_path)
    total = int(cap_o.get(cv2.CAP_PROP_FRAME_COUNT))
    deltas = []
    for frac in (0.1, 0.25, 0.4, 0.55, 0.7, 0.85, 0.95):
        f = min(int(total * frac), total - 8)
        cap_p.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, p = cap_p.read()
        if not ok:
            continue
        diffs = []
        for df in range(-2, 3):
            cap_o.set(cv2.CAP_PROP_POS_FRAMES, f + df)
            ok, o = cap_o.read()
            if not ok:
                continue
            o = cv2.resize(o, (p.shape[1], p.shape[0]),
                           interpolation=cv2.INTER_AREA)
            diffs.append((df, float(np.abs(
                p.astype(np.float32) - o.astype(np.float32)).mean())))
        orden = sorted(diffs, key=lambda t: t[1])
        if len(orden) >= 2 and orden[1][1] - orden[0][1] > 0.4:
            deltas.append(orden[0][0])
    delta = int(np.median(deltas)) if deltas else 0
    with open(cal, "w") as fh:
        json.dump({"delta": delta, "muestras": deltas}, fh)
    cap_p.release()
    cap_o.release()
    return delta


def procesar(nombre):
    video_path = os.path.join(estado.VIDEOS, nombre + ".mkv")
    proxy_dir = os.path.join(estado.VIDEOS, ".proxy")
    os.makedirs(proxy_dir, exist_ok=True)
    proxy_path = os.path.join(proxy_dir, nombre + "_h540.mp4")
    if not os.path.exists(proxy_path):
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-vf", "scale=-2:540",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-an",
             proxy_path], check=True, capture_output=True)
    delta = calibrar_offset(proxy_path, video_path)
    cap = cv2.VideoCapture(proxy_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = float(cap.get(cv2.CAP_PROP_FPS)) or 20.0
    cap.release()

    run_dir = os.path.join(estado.PROCESADOS, nombre)
    os.makedirs(run_dir, exist_ok=True)
    conn = sqlite3.connect(os.path.join(run_dir, "procesamiento.db"))
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS meta "
                "(k TEXT PRIMARY KEY, v TEXT)")
    cur.execute("INSERT OR REPLACE INTO meta VALUES ('proxy_offset', ?)",
                (str(delta),))
    cur.execute("INSERT OR REPLACE INTO meta VALUES ('fps', ?)",
                (str(fps),))
    cur.execute("INSERT OR REPLACE INTO meta VALUES ('total_frames', ?)",
                (str(total),))
    conn.commit()
    conn.close()
    print(f"proxy ok: {nombre} (delta {delta:+d}, fps {fps}, total {total})")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video", required=True)
    args = p.parse_args()
    procesar(args.video)


if __name__ == "__main__":
    main()
