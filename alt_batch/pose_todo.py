#!/usr/bin/env python3
"""Re-corre la etapa pose (un solo gate cargado) + regenera el PDF sobre
todos los runs en <base> que tengan procesamiento.db. Uso tipico: aplicar
un checkpoint nuevo del pose sin rehacer rango_extraer ni OCR."""
import argparse
import glob
import os
import sys

import pose_evento
import reporte
from pose_gate import PoseGate


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="procesados")
    p.add_argument("--videos", default="videos")
    p.add_argument("--model", default=None,
                   help="checkpoint del pose (default: pose_gate.MODEL_PATH)")
    p.add_argument("--solo", default=None, help="substring para filtrar")
    p.add_argument("--max", type=int, default=0)
    p.add_argument("--sin-pdf", action="store_true",
                   help="solo pose, sin regenerar el PDF")
    args = p.parse_args()

    runs = sorted(glob.glob(os.path.join(args.base, "*")))
    runs = [r for r in runs
            if os.path.isdir(r) and
            os.path.exists(os.path.join(r, "procesamiento.db"))]
    if args.solo:
        runs = [r for r in runs if args.solo in r]
    if args.max:
        runs = runs[:args.max]

    gate = PoseGate(model_path=args.model)
    gate.warmup()

    ok, fallos = 0, 0
    for i, run in enumerate(runs, 1):
        video = os.path.join(args.videos, os.path.basename(run) + ".mkv")
        if not os.path.exists(video):
            print(f"[{i}/{len(runs)}] SIN VIDEO: {os.path.basename(run)}",
                  flush=True)
            fallos += 1
            continue
        print(f"[{i}/{len(runs)}] pose: {os.path.basename(run)}", flush=True)
        try:
            pose_evento.procesar_run(run, video, gate)
            if not args.sin_pdf:
                sys.argv = ["reporte.py", "--run", run, "--video", video]
                reporte.main()
            ok += 1
        except Exception as e:
            fallos += 1
            print(f"    FALLO: {type(e).__name__}: {e}", flush=True)

    print(f"\nTOTAL: {ok} ok, {fallos} fallos", flush=True)


if __name__ == "__main__":
    main()
