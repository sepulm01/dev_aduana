#!/usr/bin/env python3
"""Etapa pose por evento: sobre la foto 4K del reporte de CADA camion,
corre el pose (4 kpts, sistema_cierre) y para cada cierre detectado asocia
el tipo de sello (con/sin, clases 0/1 del YOLOv9) al keypoint #3
(indice 2, el "tercer" keypoint).

Resultado en la tabla pose_cierres del run:
  camion, cierre, frame, kpt3_x, kpt3_y, kpt3_conf, kpt_mean,
  sello_cls (0=con, 1=sin, -1=sin identificar), sello_conf, dist

La posicion del cierre la da el pose; la clase con/sin la da el YOLOv9
(asociacion: kpt3 dentro del bbox del sello expandido --tol x su tamano,
nearest, desempate por conf). No dibuja nada."""
import argparse
import os
import sqlite3

import cv2

from foto_util import foto_frame, leer_frame_original
from pose_gate import PoseGate


def procesar_run(run, video, gate, tol=1.0):
    """Corre la etapa pose sobre UN run ya procesado (dets+codigos en DB)
    usando un gate ya cargado, y actualiza la tabla pose_cierres."""
    db = os.path.join(run, "procesamiento.db")
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS pose_cierres (
        camion INTEGER, cierre INTEGER, frame INTEGER,
        kpt3_x REAL, kpt3_y REAL, kpt3_conf REAL, kpt_mean REAL,
        sello_cls INTEGER, sello_conf REAL, dist REAL)""")
    cur.execute("DELETE FROM pose_cierres")

    rangos = {r[0]: (r[1], r[2]) for r in cur.execute(
        "SELECT idx, inicio, fin FROM rangos")}
    codigos = {}
    for r in cur.execute("SELECT camion, frames FROM codigos ORDER BY orden"):
        codigos.setdefault(r[0], []).append(r[1])
    row = cur.execute("SELECT v FROM meta WHERE k='proxy_offset'").fetchone()
    delta = int(row[0]) if row else 0

    cap = cv2.VideoCapture(video)
    base = os.path.splitext(os.path.basename(video))[0]
    proxy_path = os.path.join(os.path.dirname(os.path.abspath(video)),
                              ".proxy", base + "_h540.mp4")
    cap_proxy = cv2.VideoCapture(proxy_path) if os.path.exists(proxy_path) \
        else None

    total_cierres = 0
    for camion, (r0, r1) in sorted(rangos.items()):
        f = foto_frame(cur, camion, r0, r1, codigos.get(camion, []))
        if f is None:
            print(f"  camion {camion}: sin foto de reporte")
            continue
        img = leer_frame_original(cap, f, delta, cap_proxy)
        if img is None:
            print(f"  camion {camion}: frame {f} ilegible")
            continue
        H, W = img.shape[:2]
        dets = gate.detect(img)
        sellos = cur.execute(
            "SELECT cls,x1,y1,x2,y2,conf FROM dets WHERE frame=? "
            "AND cls IN (0,1)", (f,)).fetchall()
        lineas = []
        for i, det in enumerate(sorted(dets, key=lambda d: -d[4])):
            kpts = gate.kpts_de(det)
            nx, ny = float(kpts[2][0]) / W, float(kpts[2][1]) / H
            k3c = float(kpts[2][2])
            kmean = float(kpts[:, 2].mean())
            best = None
            for cls, x1, y1, x2, y2, conf in sellos:
                bw, bh = x2 - x1, y2 - y1
                ex1, ey1 = x1 - tol * bw, y1 - tol * bh
                ex2, ey2 = x2 + tol * bw, y2 + tol * bh
                if not (ex1 <= nx <= ex2 and ey1 <= ny <= ey2):
                    continue
                cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                dist = ((nx - cx) ** 2 + (ny - cy) ** 2) ** 0.5
                if best is None or dist < best[0] - 1e-9 or \
                        (abs(dist - best[0]) < 1e-9 and conf > best[1]):
                    best = (dist, conf, cls)
            sello_cls = best[2] if best else -1
            sello_conf = best[1] if best else 0.0
            dist = best[0] if best else -1.0
            cur.execute(
                "INSERT INTO pose_cierres VALUES (?,?,?,?,?,?,?,?,?,?)",
                (camion, i, f, nx, ny, k3c, kmean,
                 sello_cls, sello_conf, dist))
            total_cierres += 1
            texto = ("CON SELLO" if sello_cls == 0 else
                     "SIN SELLO" if sello_cls == 1 else "sin identificar")
            lineas.append(f"cierre#{i} kpt3={texto} "
                          f"(sello_conf={sello_conf:.2f})")
        print(f"  camion {camion} (frame {f}): "
              f"{len(dets)} cierres | " + "; ".join(lineas))

    conn.commit()
    conn.close()
    cap.release()
    if cap_proxy is not None:
        cap_proxy.release()
    print(f"pose_cierres: {total_cierres} filas en {db}")
    return total_cierres


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run", required=True)
    p.add_argument("--video", required=True)
    p.add_argument("--model", default=None)
    p.add_argument("--tol", type=float, default=1.0,
                   help="expansion del bbox del sello para asociar el kpt3")
    args = p.parse_args()

    gate = PoseGate(model_path=args.model)
    gate.warmup()
    procesar_run(args.run, args.video, gate, tol=args.tol)


if __name__ == "__main__":
    main()
