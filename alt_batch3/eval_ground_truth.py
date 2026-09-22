#!/usr/bin/env python3
"""Harness de evaluacion con ground truth (sabana DataPilot).

Mide, para un dia dado, la calidad del sistema de camaras contra la sabana:

- exacto: codigo publicado identico al real
- errada_1_3: publicado con 1-3 caracteres de error
- cruda_correcta: el codigo correcto SI esta en las lecturas crudas pero
  el informe publico otro (error de consenso)
- lecturas_todas_erradas: hubo lecturas cerca pero ninguna acerto (>3)
- sin_lectura: ninguna actividad de lectura cerca
- eventos_extra: contenedores del informe sin contraparte temporal en la
  sabana (falsos positivos)

Uso:
  python eval_ground_truth.py --xlsx sabana.xlsx \
      --informe informes/b3/containers.json \
      --raw-cam1 raw/cam1.jsonl --raw-cam2 raw/cam2.jsonl \
      --fecha 2026-09-21 [--rango 09:00-17:00]
"""
import argparse
import csv
import datetime as dt
import json
import sys
from zoneinfo import ZoneInfo

import pandas as pd

TZ = ZoneInfo("America/Santiago")
TOL = 15.0     # minutos: ventana temporal del pareo
TOL_RAW = 25.0  # minutos: ventana para lecturas crudas


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


def _parse(s):
    s = str(s).strip()
    for fmt in ("%d-%m-%Y %H:%M", "%d-%m-%Y %H:%M:%S",
                "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s[:19], fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def cargar_xlsx(path, columna="contenedor", columna_fecha="fecha_ingreso_real"):
    df = pd.read_excel(path, header=0)
    out = {}
    for _, row in df.iterrows():
        cod = str(row.get(columna, "")).strip().upper()
        if len(cod) != 11 or not cod[:4].isalpha() or not cod[4:].isdigit():
            continue
        t = _parse(row.get(columna_fecha, ""))
        if t is None:
            for col in ("fecha_bajada_camion", "fecha_termino_travesia"):
                t = _parse(row.get(col, ""))
                if t is not None:
                    break
        out.setdefault(cod, []).append(t)
    return out


def cargar_informe(path, dia):
    ini = dt.datetime.combine(dia, dt.time(0, 0), tzinfo=TZ)
    fin = ini + dt.timedelta(days=1)
    eventos = []
    for r in json.load(open(path)):
        t = dt.datetime.fromtimestamp(r["ts_inicio"], tz=TZ)
        if ini <= t < fin and r.get("codigo"):
            eventos.append({"codigo": r["codigo"], "ts": t,
                            "id": r.get("id")})
    return eventos


def cargar_crudas(paths):
    """Indice codigo -> [(ts, cam, tier)] y lista de actividad (ts, cam)."""
    crudas = {}
    actividad = []
    for cam, path in enumerate(paths, 1):
        dets = {}
        with open(path) as fh:
            for ln in fh:
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if e["tipo"] == "det":
                    dets[e["seq"]] = e["ts"]
                elif e["tipo"] == "ocr":
                    ts = dets.get(e["seq"])
                    if ts is None:
                        continue
                    for cod, tier in e.get("codigos", []):
                        crudas.setdefault(cod, []).append((ts, cam, tier))
                    actividad.append((ts, cam))
    return crudas, actividad


def evaluar(xlsx, eventos, crudas, actividad, dia, rango=None):
    ini = dt.datetime.combine(dia, dt.time(0, 0), tzinfo=TZ)
    fin = ini + dt.timedelta(days=1)
    if rango:
        h0, m0, h1, m1 = rango
        ini = dt.datetime.combine(dia, dt.time(h0, m0), tzinfo=TZ)
        fin = dt.datetime.combine(dia, dt.time(h1, m1), tzinfo=TZ)
    xlsx = {c: ts for c, ts in xlsx.items()
            if ts and ini <= ts[0] < fin}
    eventos = [e for e in eventos if ini <= e["ts"] < fin]
    crudas = {c: [(ts, cam, tier) for ts, cam, tier in v
                  if ini <= dt.datetime.fromtimestamp(ts, tz=TZ) < fin]
              for c, v in crudas.items()}
    actividad = [ts for ts, cam in actividad if ini <=
                 dt.datetime.fromtimestamp(ts, tz=TZ) < fin]

    clases = {}
    usados = set()
    for cod, tiempos in xlsx.items():
        t = min(tiempos, key=lambda x: x.timestamp())
        cands = sorted(
            ((e["codigo"], e["ts"], _lev(cod, e["codigo"]), i)
             for i, e in enumerate(eventos)
             if abs((e["ts"] - t).total_seconds()) <= TOL * 60),
            key=lambda x: (x[2], abs((x[1] - t).total_seconds())))
        if cands and cands[0][2] <= 3:
            usados.add(cands[0][3])
            clases.setdefault("exacto" if cands[0][2] == 0 else "errada_1_3",
                              []).append(cod)
            continue
        if any(ini <= dt.datetime.fromtimestamp(ts, tz=TZ) < fin
               and abs(ts - t.timestamp()) <= TOL_RAW * 60
               for ts, _, _ in crudas.get(cod, [])):
            # el codigo correcto se leyo en bruto; si ademas se publico
            # (en una ventana mas amplia) entonces el sistema acerto
            pub = [e for e in eventos
                   if _lev(cod, e["codigo"]) <= 2 and
                   abs((e["ts"] - t).total_seconds()) <= TOL_RAW * 60]
            if pub:
                clases.setdefault("exacto", []).append(cod)
            else:
                clases.setdefault("cruda_correcta", []).append(cod)
            continue
        if any(abs(ts - t.timestamp()) <= TOL * 60 for ts in actividad):
            clases.setdefault("lecturas_todas_erradas", []).append(cod)
        else:
            clases.setdefault("sin_lectura", []).append(cod)

    # un evento del informe es "extra" solo si NINGUNA fila de la sabana
    # paso en su ventana temporal (independiente del codigo)
    contraparte = set()
    for e in eventos:
        for cod, tiempos in xlsx.items():
            t = min(tiempos, key=lambda x: x.timestamp())
            if abs((e["ts"] - t).total_seconds()) <= TOL * 60:
                contraparte.add(e["id"])
                break
    extras = [e for e in eventos if e["id"] not in contraparte]
    return clases, extras, len(eventos)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--xlsx", required=True)
    p.add_argument("--informe", required=True)
    p.add_argument("--raw-cam1", required=True)
    p.add_argument("--raw-cam2", required=True)
    p.add_argument("--fecha", required=True, help="YYYY-MM-DD")
    p.add_argument("--rango", default="", help="HH:MM-HH:MM (opcional)")
    p.add_argument("--salida-csv", default="")
    args = p.parse_args()

    dia = dt.date.fromisoformat(args.fecha)
    rango = None
    if args.rango:
        a, b = args.rango.split("-")
        h0, m0 = map(int, a.split(":"))
        h1, m1 = map(int, b.split(":"))
        rango = (h0, m0, h1, m1)

    xlsx = cargar_xlsx(args.xlsx)
    eventos = cargar_informe(args.informe, dia)
    crudas, actividad = cargar_crudas([args.raw_cam1, args.raw_cam2])
    clases, extras, total_ev = evaluar(xlsx, eventos, crudas, actividad,
                                       dia, rango)

    total = sum(len(v) for v in clases.values())
    print(f"=== evaluacion {args.fecha}"
          + (f" rango {args.rango}" if rango else "") + " ===")
    print(f"filas sabana: {total} | eventos del informe: {total_ev}")
    orden = [("exacto", "publicado identico"),
             ("errada_1_3", "publicado con 1-3 chars de error"),
             ("cruda_correcta", "correcto en bruto, consenso publico otro"),
             ("lecturas_todas_erradas", "lecturas presentes, todas >3"),
             ("sin_lectura", "ninguna actividad de lectura cerca")]
    for k, desc in orden:
        n = len(clases.get(k, []))
        print(f"  {k:24s} {n:4d} ({n/total*100:5.1f}%)  {desc}")
    print(f"  eventos sin contraparte en sabana: {len(extras)}"
          f" (precision aprox. {total/(total+len(extras))*100:.0f}%)")

    if args.salida_csv:
        with open(args.salida_csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["codigo", "clase"])
            for k, _ in orden:
                for cod in sorted(clases.get(k, [])):
                    w.writerow([cod, k])
        print(f"CSV: {args.salida_csv}")


if __name__ == "__main__":
    main()
