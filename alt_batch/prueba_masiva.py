#!/usr/bin/env python3
"""Prueba masiva: por cada video de videos/, genera el Ground Truth (GT,
inferencia cada N frames a 4K/1280) y corre rango_extraer.py para comparar
los rangos detectados contra el GT. Registra todo en test_report/.

Salidas:
  test_report/gt_<video>.json          GT (rangos + conf truck por muestra)
  test_report/run_<video>/             corrida de rango_extraer (DB + JPEGs)
  test_report/comparativa.csv          una fila por video con las metricas
"""
import argparse
import csv
import glob
import json
import os
import sqlite3
import subprocess
import sys
import time



def solapa(a, b):
    return a[0] <= b[1] and b[0] <= a[1]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--videos", default="videos/*.mkv")
    p.add_argument("--cada", type=int, default=3)
    p.add_argument("--out", default="test_report")
    p.add_argument("--solo", default=None, help="substring para filtrar videos")
    p.add_argument("--max", type=int, default=0, help="limitar a los primeros N videos")
    args = p.parse_args()

    os.makedirs(args.out, exist_ok=True)
    videos = sorted(glob.glob(args.videos))
    if args.solo:
        videos = [v for v in videos if args.solo in v]
    if args.max:
        videos = videos[:args.max]
    print(f"videos a probar: {len(videos)}")

    csv_path = os.path.join(args.out, "comparativa.csv")
    nuevo = not os.path.exists(csv_path)
    fcsv = open(csv_path, "a", newline="")
    wr = csv.writer(fcsv)
    if nuevo:
        wr.writerow([
            "video", "n_gt", "n_det", "matched", "missed", "extras",
            "gt_merged_in_det", "dini_med", "dfin_med", "dini_mean", "dfin_mean",
            "gt_inferencias", "gt_s", "run_llamadas", "run_s",
        ])

    for video in videos:
        nombre = os.path.splitext(os.path.basename(video))[0]
        gt_path = os.path.join(args.out, f"gt_{nombre}.json")
        run_dir = os.path.join(args.out, f"run_{nombre}")

        # ---- GT ----
        if os.path.exists(gt_path):
            gt = json.load(open(gt_path))
            print(f"[GT] reutilizado {nombre}")
        else:
            t0 = time.time()
            subprocess.run(
                [sys.executable, "gt_rangos.py", "--video", video,
                 "--cada", str(args.cada), "--out", args.out],
                check=True, capture_output=True)
            gt = json.load(open(gt_path))
            print(f"[GT] {nombre}: {len(gt['rangos'])} rangos en {time.time()-t0:.0f}s")

        # ---- rango_extraer ----
        t0 = time.time()
        subprocess.run(
            [sys.executable, "rango_extraer.py", "--video", video,
             "--output", run_dir, "--refinar"],
            check=True, capture_output=True)
        t_run = time.time() - t0

        # ---- comparar ----
        conn = sqlite3.connect(os.path.join(run_dir, "procesamiento.db"))
        det = conn.execute("SELECT inicio, fin FROM rangos ORDER BY idx").fetchall()
        run_llamadas = conn.execute(
            "SELECT yolo_llamadas FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        run_llamadas = run_llamadas[0] if run_llamadas else -1

        gt_r = [tuple(r) for r in gt["rangos"]]
        usados = set()
        matched, missed, extras = 0, 0, 0
        merges = 0
        dinis, dfins = [], []
        for g in gt_r:
            cands = [i for i, d in enumerate(det)
                     if solapa(g, d) and i not in usados]
            if not cands:
                missed += 1
                continue
            i = cands[0]
            usados.add(i)
            matched += 1
            n_gt_dentro = sum(1 for g2 in gt_r if solapa(g2, det[i]))
            if n_gt_dentro > 1:
                merges += 1
            dinis.append(det[i][0] - g[0])
            dfins.append(det[i][1] - g[1])
        extras = len(det) - len(usados)

        dinis.sort()
        dfins.sort()
        def med(x):
            return x[len(x) // 2] if x else None
        fila = [
            nombre, len(gt_r), len(det), matched, missed, extras,
            merges,
            med(dinis), med(dfins),
            round(sum(dinis) / len(dinis), 1) if dinis else None,
            round(sum(dfins) / len(dfins), 1) if dfins else None,
            gt["inferencias"], gt["elapsed_s"], run_llamadas, round(t_run, 1),
        ]
        wr.writerow(fila)
        fcsv.flush()
        print(f"[COMP] {nombre}: gt={len(gt_r)} det={len(det)} matched={matched} "
              f"missed={missed} extras={extras} merges={merges} "
              f"dini_med={med(dinis)} dfin_med={med(dfins)} | run {t_run:.0f}s")

    fcsv.close()
    print(f"\ncomparativa en {csv_path}")


if __name__ == "__main__":
    main()
