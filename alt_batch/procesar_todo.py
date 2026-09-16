#!/usr/bin/env python3
"""Batch: procesa TODOS los videos de videos/ con el pipeline completo
(rango_extraer -> OCR -> reporte PDF). Reanudable: salta videos cuyo PDF
ya existe. Un directorio de salida por video en procesados/."""
import argparse
import glob
import os
import subprocess
import sys
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--videos", default="videos/*.mkv")
    p.add_argument("--out", default="procesados")
    p.add_argument("--solo", default=None, help="substring para filtrar")
    p.add_argument("--max", type=int, default=0, help="limitar a N videos")
    p.add_argument("--desde", type=int, default=0,
                   help="saltar los primeros N videos ordenados")
    p.add_argument("--force", action="store_true",
                   help="reprocesar aunque el PDF ya exista")
    args = p.parse_args()

    videos = sorted(glob.glob(args.videos))
    if args.solo:
        videos = [v for v in videos if args.solo in v]
    if args.desde:
        videos = videos[args.desde:]
    if args.max:
        videos = videos[:args.max]

    os.makedirs(args.out, exist_ok=True)
    python = os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                          "python")

    ok, fallos, saltados = 0, 0, 0
    t0_total = time.time()
    for i, video in enumerate(videos, 1):
        nombre = os.path.splitext(os.path.basename(video))[0]
        salida = os.path.join(args.out, nombre)
        pdf = os.path.join(salida, f"reporte_{os.path.basename(video)}.pdf")
        if os.path.exists(pdf) and not args.force:
            print(f"[{i}/{len(videos)}] SALTADO (pdf existe): {nombre}",
                  flush=True)
            saltados += 1
            continue
        t0 = time.time()
        print(f"[{i}/{len(videos)}] INICIO: {nombre}", flush=True)
        r = subprocess.run(
            [python, "procesar_video.py", "--video", video,
             "--output", salida],
            capture_output=True, text=True)
        dt = time.time() - t0
        if r.returncode == 0 and os.path.exists(pdf):
            ok += 1
            print(f"[{i}/{len(videos)}] OK {dt:.0f}s: {nombre}", flush=True)
        else:
            fallos += 1
            print(f"[{i}/{len(videos)}] FALLO {dt:.0f}s: {nombre} "
                  f"(rc={r.returncode})", flush=True)
            for line in (r.stderr or "").splitlines()[-5:]:
                print("    | " + line, flush=True)

    print(f"\nTOTAL: {ok} ok, {fallos} fallos, {saltados} saltados, "
          f"{time.time() - t0_total:.0f}s", flush=True)


if __name__ == "__main__":
    main()
