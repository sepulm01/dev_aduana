#!/usr/bin/env python3
"""Mineria de frames donde el pose fallo (4 kpts, sistema_cierre).

Recorre todos los runs en <base> (procesados/*) y, sobre cada JPEG 4K
guardado (processed_*.jpg), corre el pose sobre el frame completo.

Un frame se extrae (a 1080p, sin marcas) si:
  - el pose NO detecta ningun cierre Y el YOLOv9 detecto sellos
    (cls 0/1 en dets) en ese frame, o
  - el pose detecta cierres pero alguno tiene conf media de keypoints
    < --conf (40%).

Salida: una sola carpeta plana <out> con un JPG por frame:
  <video>_f<frame>.jpg  (1920x1080, q90)
"""
import argparse
import glob
import os
import sqlite3

import cv2
import numpy as np

from pose_gate import PoseGate


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="procesados")
    p.add_argument("--out", default="minados/frames_1080p")
    p.add_argument("--model", default=None)
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--conf", type=float, default=0.40,
                   help="umbral de conf media de kpts: bajo esto se extrae")
    p.add_argument("--conf-nms", type=float, default=0.25,
                   help="conf minima de deteccion del pose")
    p.add_argument("--solo", default=None, help="substring para filtrar videos")
    p.add_argument("--max-videos", type=int, default=0)
    p.add_argument("--max-frames", type=int, default=0,
                   help="limite total de frames evaluados (pruebas)")
    args = p.parse_args()

    gate = PoseGate(model_path=args.model, imgsz=args.imgsz,
                    conf_thres=args.conf_nms)
    gate.warmup()
    print(f"pose gate: imgsz={gate.imgsz} stride={gate.stride} "
          f"conf_nms={args.conf_nms} umbral_kpts={args.conf}")

    runs = sorted(glob.glob(os.path.join(args.base, "*")))
    runs = [r for r in runs
            if os.path.isdir(r) and
            os.path.exists(os.path.join(r, "procesamiento.db"))]
    if args.solo:
        runs = [r for r in runs if args.solo in r]
    if args.max_videos:
        runs = runs[:args.max_videos]

    os.makedirs(args.out, exist_ok=True)

    totales = {"frames": 0, "extraidos": 0, "ok": 0, "sin_deteccion": 0,
               "baja_conf": 0, "videos": 0, "videos_sin_frames": 0}
    evaluados = 0

    for run in runs:
        video = os.path.basename(run)
        db = os.path.join(run, "procesamiento.db")
        conn = sqlite3.connect(db)
        cur = conn.cursor()
        jpgs = sorted(glob.glob(os.path.join(run, "processed_*.jpg")))
        if not jpgs:
            conn.close()
            totales["videos_sin_frames"] += 1
            continue
        st = {"frames": len(jpgs), "extraidos": 0, "ok": 0,
              "sin_deteccion": 0, "baja_conf": 0}
        for jpg in jpgs:
            if args.max_frames and evaluados >= args.max_frames:
                break
            evaluados += 1
            frame = int(os.path.basename(jpg).split("_")[1].split(".")[0])
            img = cv2.imread(jpg)
            if img is None:
                continue
            dets = gate.detect(img)
            if len(dets) == 0:
                tiene_sellos = cur.execute(
                    "SELECT 1 FROM dets WHERE frame=? AND cls IN (0,1) "
                    "LIMIT 1", (frame,)).fetchone()
                if not tiene_sellos:
                    st["ok"] += 1
                    continue
                resultado = "sin_deteccion"
            else:
                medias = [float(gate.kpts_de(d)[:, 2].mean())
                          for d in dets]
                if min(medias) >= args.conf:
                    st["ok"] += 1
                    continue
                resultado = "baja_conf"
            img1080 = cv2.resize(img, (1920, 1080),
                                 interpolation=cv2.INTER_AREA)
            cv2.imwrite(os.path.join(args.out, f"{video}_f{frame:06d}.jpg"),
                        img1080, [cv2.IMWRITE_JPEG_QUALITY, 90])
            st["extraidos"] += 1
            st[resultado] += 1
        conn.close()
        totales["videos"] += 1
        for k in ("frames", "extraidos", "ok", "sin_deteccion", "baja_conf"):
            totales[k] += st[k]
        print(f"{video}: frames={st['frames']} extraidos={st['extraidos']} "
              f"ok={st['ok']} sin={st['sin_deteccion']} "
              f"baja={st['baja_conf']}", flush=True)
        if args.max_frames and evaluados >= args.max_frames:
            break

    print(f"\nTOTAL: videos={totales['videos']} "
          f"(sin frames={totales['videos_sin_frames']}) "
          f"frames={totales['frames']} | extraidos={totales['extraidos']} "
          f"(sin_deteccion={totales['sin_deteccion']} "
          f"baja_conf={totales['baja_conf']}) ok={totales['ok']}")
    print(f"salida: {args.out}")


if __name__ == "__main__":
    main()
