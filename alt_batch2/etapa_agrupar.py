#!/usr/bin/env python3
"""Etapa agrupar: agrupacion CONTINUA de pasadas por camara usando tiempo
global (hora de inicio del video + cuadro/fps).

La pasada que llega al final del video queda ABIERTA; con el siguiente
video de la misma camara se confirma silencio (gap) o se continua, de modo
que un camion cortado entre dos videos queda como UNA sola pasada.

Las pasadas cerradas con duracion >= UMBRAL_ESTACIONADO se marcan
estacionada (no se registran)."""
import argparse
import os
import sqlite3

import estado


def cargar_rangos(nombre):
    db = os.path.join(estado.PROCESADOS, nombre, "procesamiento.db")
    conn = sqlite3.connect(db)
    rangos = conn.execute(
        "SELECT idx, inicio, fin FROM rangos ORDER BY inicio").fetchall()
    meta = {k: v for k, v in conn.execute(
        "SELECT k, v FROM meta")}
    conn.close()
    fps = float(meta.get("fps", 20.0))
    total = int(meta.get("total_frames", 0)) or 0
    if total == 0:
        import cv2
        cap = cv2.VideoCapture(os.path.join(estado.VIDEOS, nombre + ".mkv"))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
    return rangos, fps, total


def agrupar(nombre):
    estado.crear_tablas()
    camara, ts = estado.parse_nombre(nombre)
    rangos, fps, total = cargar_rangos(nombre)
    with estado.FileLock("agrupar"):
        return _agrupar_lock(camara, ts, nombre, rangos, fps, total)


def _agrupar_lock(camara, ts, nombre, rangos, fps, total):
    conn = estado.conectar()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT id, t_inicio, t_fin FROM pasadas WHERE camara=? "
        "AND estado='abierta' ORDER BY id LIMIT 1", (camara,)).fetchone()
    pid = row[0] if row else None
    if pid is not None and ts < row[1]:
        conn.close()
        raise RuntimeError(
            f"{nombre}: video anterior a la pasada abierta de cam{camara} "
            f"(agrupar debe correr en orden cronologico por camara)")
    # defensa: pasadas abiertas huerfanas (su video ya termino hace mas
    # de GAP y el silencio esta cronologicamente confirmado) se cierran
    for (hp,) in cur.execute(
            "SELECT id FROM pasadas WHERE camara=? AND estado='abierta' "
            "AND t_fin + ? < ?", (camara, estado.GAP_CONTINUIDAD,
                                  ts)).fetchall():
        cerrar(hp)
        if hp == pid:
            pid = None
    t_fin_video = ts + total / fps

    def nueva(ti, tf):
        cur.execute(
            "INSERT INTO pasadas (camara, t_inicio, t_fin, estado) "
            "VALUES (?,?,?,'abierta')", (camara, ti, tf))
        return cur.lastrowid

    def cerrar(p):
        t0, tf = cur.execute(
            "SELECT t_inicio, t_fin FROM pasadas WHERE id=?", (p,)).fetchone()
        est = 1 if (tf - t0) >= estado.UMBRAL_ESTACIONADO else 0
        cur.execute("UPDATE pasadas SET estado='cerrada', estacionada=? "
                    "WHERE id=?", (est, p))
        print(f"  pasada {p} cam{camara} cerrada "
              f"({tf - t0:.0f}s{' ESTACIONADA' if est else ''})")

    for idx, i, f in rangos:
        ti = ts + i / fps
        tf = ts + f / fps
        if pid is None:
            pid = nueva(ti, tf)
        else:
            t_fin = cur.execute(
                "SELECT t_fin FROM pasadas WHERE id=?", (pid,)).fetchone()[0]
            if ti - t_fin > estado.GAP_CONTINUIDAD:
                cerrar(pid)
                pid = nueva(ti, tf)
        cur.execute(
            "INSERT OR IGNORE INTO pasada_rangos VALUES (?,?,?,?,?)",
            (pid, nombre, idx, i, f))
        cur.execute("UPDATE pasadas SET t_fin=? WHERE id=?", (tf, pid))

    if pid is not None:
        t_fin = cur.execute(
            "SELECT t_fin FROM pasadas WHERE id=?", (pid,)).fetchone()[0]
        if t_fin <= t_fin_video - estado.GAP_CONTINUIDAD:
            cerrar(pid)
    conn.commit()
    conn.close()
    print(f"agrupar ok: {nombre} ({len(rangos)} rangos)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video", required=True)
    args = p.parse_args()
    agrupar(args.video)


if __name__ == "__main__":
    main()
