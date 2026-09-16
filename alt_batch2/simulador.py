#!/usr/bin/env python3
"""Simulador del generador de videos: toma los videos historicos de
alt_batch/videos y los va depositando en llegada/ como si llegaran por
FTP/rsync, en orden cronologico, un par (cam1+cam2) cada --cada segundos.

- Copia realista: archivo temporal .part -> renombrado final.
- cam2 llega con un retraso aleatorio <= --jitter segundos tras cam1.
- Reanudable: omite pares ya presentes en llegada/ o videos/."""
import argparse
import json
import os
import random
import shutil
import time

import estado

ESTADO_ARCHIVO = os.path.join(estado.BASE, "simulador_estado.json")


def pares_disponibles(fuente):
    pares = {}
    for f in sorted(os.listdir(fuente)):
        if f.endswith(".mkv") and f.startswith("cam"):
            ts = f.split("_", 1)[1][:-4]
            pares.setdefault(ts, {})[f] = os.path.join(fuente, f)
    return pares


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cada", type=float, default=300.0,
                   help="segundos entre pares (300 = tiempo real)")
    p.add_argument("--jitter", type=float, default=10.0,
                   help="retraso maximo aleatorio de cam2 respecto a cam1")
    p.add_argument("--max-pares", type=int, default=None)
    p.add_argument("--fuente", default=estado.FUENTE_VIAJES)
    args = p.parse_args()

    os.makedirs(estado.LLEGADA, exist_ok=True)
    os.makedirs(estado.VIDEOS, exist_ok=True)
    pares = pares_disponibles(args.fuente)
    estado_previo = {}
    if os.path.exists(ESTADO_ARCHIVO):
        try:
            estado_previo = json.load(open(ESTADO_ARCHIVO))["enviados"]
        except Exception:
            estado_previo = {}
    enviados = dict(estado_previo)

    pendientes = [ts for ts in sorted(pares)
                  if ts not in enviados and
                  all(not (os.path.exists(os.path.join(estado.LLEGADA, f))
                           or os.path.exists(os.path.join(estado.VIDEOS, f)))
                      for f in pares[ts])]
    if args.max_pares:
        pendientes = pendientes[:args.max_pares]
    print(f"pares totales: {len(pares)} | pendientes: {len(pendientes)}")

    def guardar():
        with open(ESTADO_ARCHIVO, "w") as fh:
            json.dump({"enviados": enviados}, fh)

    siguiente = time.time()
    for ts in pendientes:
        files = pares[ts]
        cam1 = files.get(f"cam1_{ts}.mkv")
        cam2 = files.get(f"cam2_{ts}.mkv")
        dormir = siguiente - time.time()
        if dormir > 0:
            print(f"[{time.strftime('%H:%M:%S')}] esperando {dormir:.0f}s "
                  f"hasta el par {ts}")
            time.sleep(dormir)
        siguiente += args.cada
        for nombre, src in ((f"cam1_{ts}.mkv", cam1),
                            (f"cam2_{ts}.mkv", cam2)):
            if not src:
                continue
            destino = os.path.join(estado.LLEGADA, nombre)
            if os.path.exists(destino) or os.path.exists(
                    os.path.join(estado.VIDEOS, nombre)):
                continue
            print(f"[{time.strftime('%H:%M:%S')}] enviando {nombre} "
                  f"({os.path.getsize(src) / 1e6:.0f} MB)")
            shutil.copyfile(src, destino + ".part")
            os.replace(destino + ".part", destino)
            if nombre.startswith("cam1_") and cam2 and args.jitter > 0:
                time.sleep(random.uniform(0, args.jitter))
        enviados[ts] = time.time()
        guardar()
    print("simulador: no quedan pares pendientes")


if __name__ == "__main__":
    main()
