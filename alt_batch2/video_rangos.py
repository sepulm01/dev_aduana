#!/usr/bin/env python3
"""Genera un video por camara con los tramos de los rangos del nuevo
detector (etapa_detectar.py): reproduce los frames de cada rango y dibuja
los bboxes de las muestras (verde=con_sello, rojo=sin_sello, rojo grueso
=codigo) con etiqueta de video/frame/rango."""
import os
import sqlite3

import cv2

import estado

COLORES = {0: (0, 200, 0), 1: (0, 0, 255)}


def generar(cam, videos, out):
    fps = 20.0
    vw = None
    for video in videos:
        db = os.path.join(estado.PROCESADOS, video, "procesamiento.db")
        conn = sqlite3.connect(db)
        rangos = conn.execute(
            "SELECT idx, inicio, fin FROM rangos ORDER BY idx").fetchall()
        dets = {}
        for r in conn.execute("SELECT frame, cls, conf, x1,y1,x2,y2 FROM dets"):
            dets.setdefault(r[0], []).append(r[1:])
        conn.close()
        proxy = os.path.join(estado.VIDEOS, ".proxy", video + "_h540.mp4")
        cap = cv2.VideoCapture(proxy)
        for idx, r0, r1 in rangos:
            for f in range(r0, r1 + 1):
                cap.set(cv2.CAP_PROP_POS_FRAMES, f)
                ok, frame = cap.read()
                if not ok:
                    break
                if vw is None:
                    vw = cv2.VideoWriter(
                        out, cv2.VideoWriter_fourcc(*"mp4v"), fps,
                        (frame.shape[1], frame.shape[0]))
                for cls, conf, x1, y1, x2, y2 in dets.get(f, []):
                    color = (COLORES.get(cls, (0, 0, 255)))
                    grosor = 10 if cls == 3 else 5
                    cv2.rectangle(frame,
                                  (int(x1 * frame.shape[1]),
                                   int(y1 * frame.shape[0])),
                                  (int(x2 * frame.shape[1]),
                                   int(y2 * frame.shape[0])),
                                  color, grosor)
                cv2.putText(frame, f"{video} rango={idx} f={f}",
                            (14, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.1,
                            (255, 255, 0), 2)
                vw.write(frame)
        cap.release()
        print(f"{video}: {len(rangos)} rangos escritos")
    if vw is not None:
        vw.release()
    print(f"video generado: {out} ({os.path.getsize(out) / 1e6:.1f} MB)")


def main():
    videos = [
        "cam1_20260901_144704", "cam1_20260901_145203",
        "cam1_20260901_145703", "cam2_20260901_144704",
        "cam2_20260901_145203", "cam2_20260901_145703",
    ]
    os.makedirs(os.path.join(estado.BASE, "informes"), exist_ok=True)
    for cam in (1, 2):
        vids = [v for v in videos if v.startswith(f"cam{cam}_")]
        out = os.path.join(estado.BASE, "informes",
                           f"video_cam{cam}_rangos.mp4")
        generar(cam, vids, out)


if __name__ == "__main__":
    main()
