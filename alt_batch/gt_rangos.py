#!/usr/bin/env python3
"""Ground Truth de rangos de camiones: inferencia YOLO (truck) cada N frames
sobre el video ORIGINAL 4K, imgsz=1280 (maxima recall). Escribe un JSON con
los rangos y la confianza de truck por frame muestreado."""
import argparse
import json
import os
import time

import cv2

from yolo_gate import YoloGate


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video", required=True)
    p.add_argument("--cada", type=int, default=3)
    p.add_argument("--out", default="test_report")
    p.add_argument("--batch", type=int, default=8)
    args = p.parse_args()

    os.makedirs(args.out, exist_ok=True)
    nombre = os.path.splitext(os.path.basename(args.video))[0]
    salida = os.path.join(args.out, f"gt_{nombre}.json")

    t0 = time.time()
    gate = YoloGate(imgsz=1280)
    gate.warmup()
    cap = cv2.VideoCapture(args.video)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    truck_conf = {}   # frame muestreado -> conf maxima de truck (0 si no hay)
    lote_frames, lote_ids = [], []
    inferencias = 0

    def flush():
        nonlocal inferencias
        if not lote_frames:
            return
        for f, dets in zip(lote_ids, gate.detect_batch(lote_frames)):
            inferencias += 1
            truck_conf[f] = max((float(d[4]) for d in dets if int(d[5]) == 4),
                                default=0.0)
        lote_frames.clear()
        lote_ids.clear()

    for f in range(0, total, args.cada):
        ok, frame = cap.read()
        if not ok:
            break
        lote_frames.append(frame)
        lote_ids.append(f)
        if len(lote_frames) >= args.batch:
            flush()
    flush()
    cap.release()

    # rangos: muestras contiguas con truck presente; gap <= 3 muestras se une
    presentes = [f for f in sorted(truck_conf) if truck_conf[f] >= 0.40]
    rangos = []
    if presentes:
        a = b = presentes[0]
        for f in presentes[1:]:
            if f - b <= args.cada * 3:
                b = f
            else:
                rangos.append([a, b])
                a = b = f
        rangos.append([a, b])

    res = {
        "video": os.path.basename(args.video),
        "cada": args.cada,
        "total_frames": total,
        "inferencias": inferencias,
        "elapsed_s": round(time.time() - t0, 1),
        "rangos": rangos,
        "truck_conf": {str(k): v for k, v in sorted(truck_conf.items())},
    }
    with open(salida, "w") as fh:
        json.dump(res, fh)
    print(f"GT {nombre}: {len(rangos)} rangos, {inferencias} inferencias, "
          f"{res['elapsed_s']}s -> {salida}")


if __name__ == "__main__":
    main()
