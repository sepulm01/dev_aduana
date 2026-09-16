#!/usr/bin/env python3
"""Etapa deteccion v2: escaneo secuencial del proxy, inferencia cada
--muestreo frames (1s por defecto).

- Interes de muestra: presencia de cls3 (codigo) o cls0/1 (sellos).
- Rangos: abren con la primera muestra interesante y cierran tras
  --vacia muestras consecutivas sin interes.
- Estacionado por posicion: detecciones de la misma clase cuyo centro se
  desplaza menos de --disp (fraccion del ancho/alto) entre muestras
  consecutivas forman una estacion; si una estacion persiste
  >= --t-estacionado segundos, las muestras que cubre se marcan
  estacionadas.
- Un rango totalmente estacionado se descarta; los mixtos (estacionado +
  camion pasando) se parten por movimiento.
- cls2/cls4 se guardan en dets (evidencia) pero no disparan nada."""
import argparse
import os
import sqlite3
import time

import cv2
import torch

import estado
from yolo_gate import YoloGate

CLASES_INTERES = (0, 1, 3)
FPS = 20.0


def _flush(gate, buf, cur, dets_por_muestra, w, h):
    ids = [x[0] for x in buf]
    imgs = [x[1] for x in buf]
    for f, dets in zip(ids, gate.detect_batch(imgs)):
        dets_por_muestra[f] = []
        for d in dets:
            cls, conf = int(d[5]), float(d[4])
            x1, y1 = float(d[0]) / w, float(d[1]) / h
            x2, y2 = float(d[2]) / w, float(d[3]) / h
            cur.execute("INSERT INTO dets VALUES (?,?,?,?,?,?,?)",
                        (f, cls, conf, x1, y1, x2, y2))
            dets_por_muestra[f].append((cls, conf, x1, y1, x2, y2))
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def procesar(nombre, conf=0.40, imgsz=640, muestreo=30, vacia=2,
             t_estacionado=10.0, disp=0.02, batch=8):
    proxy_path = os.path.join(estado.VIDEOS, ".proxy", nombre + "_h540.mp4")
    run_dir = os.path.join(estado.PROCESADOS, nombre)
    db = os.path.join(run_dir, "procesamiento.db")
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS dets (
        frame INTEGER, cls INTEGER, conf REAL,
        x1 REAL, y1 REAL, x2 REAL, y2 REAL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS rangos (
        idx INTEGER, inicio INTEGER, fin INTEGER)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS estaciones (
        cls INTEGER, f_inicio INTEGER, f_fin INTEGER, xc REAL, yc REAL,
        dur_s REAL, parked INTEGER)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        elapsed_s REAL, yolo_llamadas INTEGER, rangos INTEGER,
        frames_barridos INTEGER)""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_dets_frame ON dets (frame)")
    cur.execute("DELETE FROM dets")
    cur.execute("DELETE FROM rangos")
    cur.execute("DELETE FROM estaciones")
    conn.commit()

    t0 = time.time()
    with estado.GpuLock():
        gate = YoloGate(model_path=None, conf_thres=conf, imgsz=imgsz)
        gate.warmup()
        cap = cv2.VideoCapture(proxy_path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
        h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        dets_por_muestra = {}
        buf = []
        for f in range(0, total, muestreo):
            cap.set(cv2.CAP_PROP_POS_FRAMES, f)
            ok, frame = cap.read()
            if not ok:
                break
            buf.append((f, frame))
            if len(buf) >= batch:
                _flush(gate, buf, cur, dets_por_muestra, w, h)
                conn.commit()
                buf = []
        if buf:
            _flush(gate, buf, cur, dets_por_muestra, w, h)
            conn.commit()

        def inferir_extra(g):
            nonlocal buf
            cap.set(cv2.CAP_PROP_POS_FRAMES, g)
            ok, frame = cap.read()
            if not ok:
                return
            buf.append((g, frame))
            if len(buf) >= batch:
                _flush(gate, buf, cur, dets_por_muestra, w, h)
                conn.commit()
                buf = []

        muestras = sorted(dets_por_muestra)
        interes = {f: any(cls in CLASES_INTERES for cls, *_ in ds)
                   for f, ds in dets_por_muestra.items()}

        # ---- rangos por interes ----
        brutos = []
        abierto = None
        ultima = None
        vacias = 0
        for f in muestras:
            if interes[f]:
                ultima = f
                vacias = 0
                if abierto is None:
                    # retroceso de 15 frames: inferir los que falten para
                    # no perder detecciones al principio del rango
                    inicio = max(0, f - 15)
                    for g in range(inicio, f):
                        if g not in dets_por_muestra:
                            inferir_extra(g)
                    abierto = inicio
            elif abierto is not None:
                vacias += 1
                if vacias >= vacia:
                    brutos.append((abierto, ultima))
                    abierto = None
        if abierto is not None:
            brutos.append((abierto, ultima))
        if buf:
            _flush(gate, buf, cur, dets_por_muestra, w, h)
            conn.commit()
        cap.release()

    # ---- estaciones (estacionado por posicion) ----
    estaciones = []
    cerradas = []
    membresia = {}
    for f in muestras:
        ds = [(cls, (x1 + x2) / 2, (y1 + y2) / 2)
              for cls, _c, x1, y1, x2, y2 in dets_por_muestra[f]
              if cls in CLASES_INTERES]
        ids_muestra = []
        renovadas = set()
        for cls, xc, yc in ds:
            best = None
            bestd = None
            for e in estaciones:
                if e["cerrada"] or e["cls"] != cls:
                    continue
                d = max(abs(xc - e["xc"]), abs(yc - e["yc"]))
                if d <= disp and (bestd is None or d < bestd):
                    best, bestd = e, d
            if best is not None:
                best["xc"], best["yc"], best["f_fin"] = xc, yc, f
                renovadas.add(id(best))
                ids_muestra.append(id(best))
            else:
                e = {"cls": cls, "f_inicio": f, "f_fin": f,
                     "xc": xc, "yc": yc, "cerrada": False}
                estaciones.append(e)
                renovadas.add(id(e))
                ids_muestra.append(id(e))
        membresia[f] = ids_muestra
        for e in estaciones:
            if not e["cerrada"] and id(e) not in renovadas:
                e["cerrada"] = True
                cerradas.append(e)
    for e in estaciones:
        if not e["cerrada"]:
            cerradas.append(e)

    parked_ids = set()
    for e in cerradas:
        dur = (e["f_fin"] - e["f_inicio"]) / FPS
        parked = dur >= t_estacionado
        if parked:
            parked_ids.add(id(e))
        cur.execute("INSERT INTO estaciones VALUES (?,?,?,?,?,?,?)",
                    (e["cls"], e["f_inicio"], e["f_fin"],
                     round(e["xc"], 4), round(e["yc"], 4), round(dur, 1),
                     int(parked)))
    conn.commit()

    def es_estacionada(f):
        ids_m = membresia.get(f)
        return bool(ids_m) and all(mid in parked_ids for mid in ids_m)

    # ---- partir por movimiento ----
    rangos = []
    for a, b in brutos:
        seg_abierto = None
        seg_ultima = None
        for f in muestras:
            if f < a or f > b:
                continue
            if not interes[f]:
                continue
            if not es_estacionada(f):
                seg_ultima = f
                if seg_abierto is None:
                    seg_abierto = max(a, f - 15)
            elif seg_abierto is not None:
                rangos.append((seg_abierto, seg_ultima))
                seg_abierto = None
        if seg_abierto is not None:
            rangos.append((seg_abierto, seg_ultima))

    for idx, (a, b) in enumerate(rangos, 1):
        cur.execute("INSERT INTO rangos VALUES (?,?,?)", (idx, a, b))
    elapsed = time.time() - t0
    cur.execute(
        "INSERT INTO runs (elapsed_s, yolo_llamadas, rangos, "
        "frames_barridos) VALUES (?,?,?,?)",
        (round(elapsed, 1), gate.calls, len(rangos), len(muestras)))
    conn.commit()
    conn.close()
    n_parked = sum(1 for e in cerradas if id(e) in parked_ids)
    print(f"{nombre}: {len(muestras)} muestras, {len(brutos)} rangos brutos, "
          f"{n_parked} estaciones estacionadas, {len(rangos)} rangos "
          f"moviles ({elapsed:.1f}s)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video", required=True)
    p.add_argument("--conf", type=float, default=0.40)
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--muestreo", type=int, default=30,
                   help="frames entre muestras (30 = 1s)")
    p.add_argument("--vacia", type=int, default=2,
                   help="muestras sin interes para cerrar el rango")
    p.add_argument("--t-estacionado", type=float, default=10.0,
                   help="segundos de persistencia para marcar estacionado")
    p.add_argument("--disp", type=float, default=0.02,
                   help="desplazamiento maximo (fraccion) entre muestras")
    p.add_argument("--batch", type=int, default=8)
    args = p.parse_args()
    procesar(args.video, args.conf, args.imgsz, args.muestreo, args.vacia,
             args.t_estacionado, args.disp, args.batch)


if __name__ == "__main__":
    main()
