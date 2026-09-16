#!/usr/bin/env python3
"""Prueba del enfoque "muestreo grueso + bisectriz" (sin MOG2) para detectar
los rangos de paso de camiones, comparado contra el ground truth de las
rafagas del run anterior (frames_cod/procesamiento.db). No guarda nada."""
import argparse
import sqlite3

import cv2

from yolo_gate import YoloGate

VIDEO = "videos/cam1_20260901_144704.mkv"
DB = "frames_cod/procesamiento.db"
PASO = 100          # intervalo del barrido grueso (frames)
CLASE = 4           # truck
UMBRAL_PX = 1500    # para derivar el ground truth de las rafagas
GAP = 6             # gap max entre mediciones para considerar la misma rafaga
TOTAL = 6000


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video", default=VIDEO)
    p.add_argument("--db", default=DB)
    p.add_argument("--paso", type=int, default=PASO)
    args = p.parse_args()

    # ---- Ground truth: rafagas de la DB ----
    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        f"SELECT frame FROM frames WHERE px > {UMBRAL_PX} ORDER BY frame").fetchall()
    conn.close()
    gt = []
    if rows:
        cur = [rows[0][0]]
        for r in rows[1:]:
            if r[0] - cur[-1] <= GAP:
                cur.append(r[0])
            else:
                gt.append((cur[0], cur[-1]))
                cur = [r[0]]
        gt.append((cur[0], cur[-1]))

    gate = YoloGate()
    gate.warmup()
    cap = cv2.VideoCapture(args.video)

    def lee(f):
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, frame = cap.read()
        return frame if ok else None

    def tiene_truck(f):
        frame = lee(f)
        if frame is None:
            return None
        _, dets = gate.has_class(frame)
        return any(int(d[5]) == CLASE for d in dets)

    # ---- Barrido grueso ----
    muestras = []
    for f in range(0, TOTAL, args.paso):
        muestras.append((f, tiene_truck(f)))
    n_pos = sum(1 for _, pos in muestras if pos)

    # ---- Bisectriz ----
    rangos = []   # lista de (entrada, salida)
    i = 0
    while i < len(muestras):
        if not muestras[i][1]:
            i += 1
            continue
        # agrupar muestras positivas consecutivas = un camion
        j = i
        while j + 1 < len(muestras) and muestras[j + 1][1]:
            j += 1
        # entrada: entre la ultima negativa y la primera positiva
        lo = muestras[i - 1][0] if i > 0 else 0
        hi = muestras[i][0]
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if tiene_truck(mid):
                hi = mid
            else:
                lo = mid
        entrada = hi
        # salida: entre la ultima positiva y la primera negativa
        lo = muestras[j][0]
        hi = muestras[j + 1][0] if j + 1 < len(muestras) else TOTAL - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if tiene_truck(mid):
                lo = mid
            else:
                hi = mid
        salida = lo
        rangos.append((entrada, salida))
        i = j + 1

    # ---- Comparacion vs GT ----
    print(f"muestras gruesas: {len(muestras)} (positivas: {n_pos})")
    print(f"rangos detectados (bisectriz): {len(rangos)}")
    print(f"GT rafagas: {len(gt)}")
    print()
    print(f"{'GT':>20} {'DETECTADO':>22} {'delta_ini':>10} {'delta_fin':>10}")
    errores_ini, errores_fin = [], []
    for gi, (g0, g1) in enumerate(gt, 1):
        solape = [(r0, r1) for r0, r1 in rangos
                  if r0 <= g1 and r1 >= g0]
        if not solape:
            print(f"camion {gi:>2}: {g0:>5}-{g1:<5} {'PERDIDO':>22}")
            continue
        r0, r1 = solape[0]
        d0, d1 = r0 - g0, r1 - g1
        errores_ini.append(d0)
        errores_fin.append(d1)
        print(f"camion {gi:>2}: {g0:>5}-{g1:<5} {r0:>6}-{r1:<6} "
              f"{d0:>10} {d1:>10}")
    extras = len(rangos) - len([1 for g0, g1 in gt
                                for r0, r1 in rangos if r0 <= g1 and r1 >= g0])
    print()
    print(f"llamadas YOLO: {gate.calls}")
    if errores_ini:
        print(f"delta entrada (frames): media {sum(errores_ini)/len(errores_ini):+.1f}, "
              f"rango {min(errores_ini)}..{max(errores_ini)}")
        print(f"delta salida (frames): media {sum(errores_fin)/len(errores_fin):+.1f}, "
              f"rango {min(errores_fin)}..{max(errores_fin)}")
    print(f"rangos detectados sin GT: {extras}")
    cap.release()


if __name__ == "__main__":
    main()
