#!/usr/bin/env python3
"""Re-OCR de los contenedores no leidos de un dia (ground truth sabana).

Por cada camion de la sabana cuyo codigo correcto NO aparece en las
lecturas crudas (clase "lecturas_todas_erradas"), se re-OCR:

1) crops guardados que nunca llegaron al OCR (drops de cola) -> /ocr
2) crops con OCR sin codigo valido -> /spotting (upscale automatico)
3) crops nuevos recortados de los frames 4K -> /ocr

Resultados: CSV reocr_batch.csv + merge al reocr.json del analizador
(la reconstruccion del informe en el proximo ciclo los aplica).

Uso (en el servidor, desde /home/martin/aduana_rt):
  python reprocesar_no_leidos.py --sabana sabana.csv --fecha 2026-09-21 \
      --raw-cam1 raw/cam1.jsonl --raw-cam2 raw/cam2.jsonl \
      [--max-crops-por-camion 8] [--max-total 2500] [--sin-feedback]
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

import cv2
import numpy as np
import requests

import ocr_codes

TZ = ZoneInfo("America/Santiago")
TOL = 25.0  # minutos de ventana alrededor del ingreso de la sabana
PAD_X2, PAD_Y2 = 0.25, 0.45  # padding para los crops nuevos desde 4K
W4, H4 = 3840, 2160


def _lev(a, b):
    if len(a) < len(b):
        return _lev(b, a)
    if len(b) == 0:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, c1 in enumerate(a):
        cur = [i + 1]
        for j, c2 in enumerate(b):
            cur.append(min(prev[j + 1] + 1, cur[j] + 1,
                           prev[j] + (c1 != c2)))
        prev = cur
    return prev[-1]


def parse_fecha(s):
    s = str(s).strip()
    for fmt in ("%d-%m-%Y %H:%M", "%d-%m-%Y %H:%M:%S",
                "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s[:19], fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def cargar_sabana(path):
    out = {}
    for f in csv.DictReader(open(path)):
        t = parse_fecha(f.get("fecha_ingreso_real", ""))
        if t is None:
            t = parse_fecha(f.get("fecha_bajada_camion", ""))
        if t:
            out[f["codigo"]] = t
    return out


def cargar_raw(paths):
    dets = {}
    ocrs = {}
    for cam, path in enumerate(paths, 1):
        det_seq = {}
        with open(path) as fh:
            for ln in fh:
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if e["tipo"] == "det":
                    det_seq[e["seq"]] = e
                elif e["tipo"] == "ocr":
                    det_seq.pop("_no_uso", None)
                    d = det_seq.get(e["seq"])
                    if d is None:
                        continue
                    ocrs[e["seq"]] = e
                    dets[e["seq"]] = (cam, d)
    # dets sin OCR tambien entran (drops de cola)
    for cam, path in enumerate(paths, 1):
        with open(path) as fh:
            for ln in fh:
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if e["tipo"] == "det" and e["seq"] not in dets:
                    dets[e["seq"]] = (cam, e)
    return dets, ocrs


def cargar_f4k(paths):
    out = []
    for cam, path in enumerate(paths, 1):
        with open(path) as fh:
            for ln in fh:
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if e["tipo"] == "frame4k":
                    out.append((cam, e))
    return out


def vl(endpoint, img, server):
    r = requests.post(server + endpoint, files={"file": img},
                      timeout=180)
    r.raise_for_status()
    return r.json().get("text", "")


def codigos_de(texto):
    return ocr_codes.extraer_codigos(ocr_codes.limpiar(texto))


def crop_4k(imagen, bbox):
    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1
    px, py = int(bw * PAD_X2), int(bh * PAD_Y2)
    cx1, cy1 = max(0, int(x1 - px)), max(0, int(y1 - py))
    cx2, cy2 = min(W4, int(x2 + px)), min(H4, int(y2 + py))
    if cx2 - cx1 < 60 or cy2 - cy1 < 60:
        return None
    return imagen[cy1:cy2, cx1:cx2]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sabana", required=True)
    p.add_argument("--fecha", required=True, help="YYYY-MM-DD")
    p.add_argument("--raw-cam1", required=True)
    p.add_argument("--raw-cam2", required=True)
    p.add_argument("--server", default="http://localhost:5003")
    p.add_argument("--max-crops-por-camion", type=int, default=8)
    p.add_argument("--max-total", type=int, default=2500)
    p.add_argument("--sin-feedback", action="store_true")
    args = p.parse_args()

    dia = dt.date.fromisoformat(args.fecha)
    ini = dt.datetime.combine(dia, dt.time(0, 0), tzinfo=TZ)
    fin = ini + dt.timedelta(days=1)

    sabana = cargar_sabana(args.sabana)
    dets, ocrs = cargar_raw([args.raw_cam1, args.raw_cam2])
    f4k = cargar_f4k([args.raw_cam1, args.raw_cam2])

    # targets: codigos de la sabana sin lectura correcta en crudo
    crudas = {}
    for seq, (cam, d) in dets.items():
        o = ocrs.get(seq)
        if not o:
            continue
        for c, tier in o.get("codigos", []):
            crudas.setdefault(c, []).append(d["ts"])
    targets = []
    for cod, t in sabana.items():
        if not (ini <= t < fin):
            continue
        if any(abs(ts - t.timestamp()) <= TOL * 60
               for ts in crudas.get(cod, [])):
            continue
        targets.append((cod, t))
    targets.sort(key=lambda x: x[1])
    print(f"[reocr] {len(targets)} camiones objetivo "
          f"({dia.strftime('%d-%m')})", flush=True)

    resultados = {}   # crop -> {"codigos": [[c,t]], "endpoint", "texto"}
    probados = set()
    n_llamadas = 0
    rescatados = []

    for cod, t in targets:
        if n_llamadas >= args.max_total:
            print("[reocr] tope de llamadas alcanzado", flush=True)
            break
        lo = t.timestamp() - TOL * 60
        hi = t.timestamp() + TOL * 60
        ventana = []
        for seq, (cam, d) in dets.items():
            if lo <= d["ts"] <= hi:
                ventana.append((cam, seq, d, ocrs.get(seq)))
        # pasada 1: crop guardado sin OCR (drop de cola)
        p1, p2, p3 = [], [], []
        for cam, seq, d, o in ventana:
            crop = d.get("crop")
            if not crop:
                continue
            if o is None:
                p1.append((cam, d, crop))
            elif not o.get("codigos"):
                p2.append((cam, d, crop))
        # pasada 3: crops nuevos desde frame 4K (dets sin crop guardado
        # o como refuerzo si no hay suficientes candidatos)
        f4k_cercanos = [(cam, e) for cam, e in f4k
                        if lo - 5 <= e["ts"] <= hi + 5]
        for cam, d, _ in (p1 + p2):
            p3.append((cam, d, None))
        if not p1 and not p2:
            for cam, seq, d, o in ventana:
                p3.append((cam, d, None))

        cola = []
        for cam, d, crop in p1:
            cola.append((cam, d, crop, "/ocr"))
        for cam, d, crop in p2:
            cola.append((cam, d, crop, "/spotting"))
        if len(cola) < args.max_crops_por_camion:
            for cam, d, crop in p3:
                if crop is None and any(
                        abs(e["ts"] - d["ts"]) <= 5
                        for ce, e in f4k_cercanos if ce == cam):
                    cola.append((cam, d, None, "/ocr"))
        cola.sort(key=lambda x: -(x[1].get("area", 0)))
        cola = [x for x in cola if x[2] not in probados]
        cola = cola[:args.max_crops_por_camion]

        for cam, d, crop, endpoint in cola:
            if n_llamadas >= args.max_total:
                break
            probados.add(crop)
            n_llamadas += 1
            try:
                if crop and os.path.exists(crop):
                    with open(crop, "rb") as fh:
                        img = fh.read()
                else:
                    frame = None
                    for ce, e in f4k_cercanos:
                        if ce == cam and abs(e["ts"] - d["ts"]) <= 5 \
                                and os.path.exists(e["path"]):
                            frame = cv2.imread(e["path"])
                            if frame is not None:
                                break
                    if frame is None:
                        continue
                    recorte = crop_4k(frame, d["bbox"])
                    if recorte is None:
                        continue
                    ok, buf = cv2.imencode(
                        ".jpg", recorte,
                        [cv2.IMWRITE_JPEG_QUALITY, 90])
                    if not ok:
                        continue
                    img = buf.tobytes()
                texto = vl(endpoint, img, args.server)
            except (requests.RequestException, OSError) as e:
                print(f"[reocr] error {e}", flush=True)
                continue
            cods = codigos_de(texto)
            clave = crop or f"4k:cam{cam}:f{d['f']}"
            resultados[clave] = {"codigos": [[c, t] for c, t, _ in cods],
                                 "endpoint": endpoint,
                                 "texto": texto[:80]}
            if cods:
                print(f"[reocr] {cod} {endpoint} -> "
                      + ", ".join(f"{c}({t})" for c, t, _ in cods),
                      flush=True)
        # rescate: algun resultado con el codigo correcto?
        for clave, r in resultados.items():
            if any(_lev(c, cod) <= 2 for c, _ in r["codigos"]):
                if cod not in rescatados:
                    rescatados.append(cod)

    print(f"[reocr] fin: {n_llamadas} llamadas, "
          f"{len(resultados)} resultados, "
          f"{len(rescatados)} camiones con codigo correcto rescatado",
          flush=True)

    with open("reocr_batch.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["clave", "endpoint", "codigos", "texto"])
        for clave, r in resultados.items():
            w.writerow([clave, r["endpoint"],
                        "|".join(f"{c}:{t}" for c, t in r["codigos"]),
                        r["texto"]])
    print("[reocr] CSV: reocr_batch.csv", flush=True)

    if not args.sin_feedback and resultados:
        cache_path = os.path.join("informes", "b3", "reocr.json")
        cache = {}
        try:
            with open(cache_path) as fh:
                cache = json.load(fh)
        except (OSError, ValueError):
            pass
        for clave, r in resultados.items():
            cache[clave] = {"codigos": r["codigos"]}
        claves = sorted(cache)[-4000:]
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as fh:
            json.dump({k: cache[k] for k in claves}, fh)
        print(f"[reocr] feedback aplicado a {cache_path}", flush=True)


if __name__ == "__main__":
    main()
