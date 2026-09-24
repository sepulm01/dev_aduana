#!/usr/bin/env python3
"""Analizador alt_batch3: construye contenedores desde la evidencia cruda.

Re-ejecuta TODO el analisis cada ciclo leyendo los JSONL de ambas camaras:

- Por camara: cluster duro por silencio (>20s) + cortes de trayectoria
  (rebobinado persistente). Los cortes por cambio de codigo SOLO se
  aplican con corroboracion de la otra camara (2 clusters en la ventana
  o una frontera de codigo similar dentro de 10s) -> evita contenedores
  fantasma por lecturas ruidosas del mismo codigo.
- Codigo por camion: votos (tier + repeticion) + re-OCR de crops sin
  lectura cuando hay ambiguedad (dos familias a <3 votos de distancia).
- Empareje cam1<->cam2: codigo (fuzzy<=2, |dt|<=60s), luego alineacion
  monotona con ventana inteligente.
- Sello: unanimidad de los picos (kpt3, conf>=0.6) por camion; fusion
  cross-camara (acuerdo -> CON/SIN SELLO, resto -> DUDA).
- Informe: solo camiones cerrados (silencio >15s), IDs fijos.

Uso: python analizador.py --raw-cam1 raw/cam1.jsonl \
       --raw-cam2 raw/cam2.jsonl --salida informes/b3
"""
import argparse
import json
import os
import sys
import time
from collections import Counter

import cv2
import requests

import ocr_codes
from conciliar import CSS, esc, sello_fusion
from video import PAD_X, PAD_Y, SERVER, _veredicto_frames

W, H = 3840, 2160
GAP_CLUSTER = 20.0
GAP_CODIGO = 8
CIERRE = 15.0
TOL_CODIGO = 60.0
TOL_AISLADO = 10.0
TOL_CONGESTION = 40.0
CERCANIA = 15.0
TOL_FRONTERA = 10.0
MARGEN_AMBIGUO = 3
RETENCION_DIAS = 7
UMBRAL_MOV = 5.0
MIN_MUESTRAS_MOV = 3

COLOR_HUE_TOL = 0.08
COLOR_V_ESTABLE = 0.35
COLOR_V_MIN = 0.12
COLOR_S_GRIS = 0.10
COLOR_DV_GRIS = 0.20
COLOR_MIN_MUESTRAS = 2
COLOR_MIN_DIVERGE = 3


def _hue_dist(h1, h2):
    d = abs(h1 - h2) % 1.0
    return min(d, 1.0 - d)


def _color_de(d):
    hsv = d.get("hsv")
    if not hsv or len(hsv) < 3 or hsv[0] is None:
        return None
    return hsv


def _color_ancla(truck):
    """Ancla de color del camion: hue mediana circular + s/v medianos.
    Devuelve (h, s, v, n) o None si hay pocas muestras."""
    cols = [c for c in (_color_de(d) for d in truck["dets"])
            if c is not None]
    if len(cols) < COLOR_MIN_MUESTRAS:
        return None
    hues = sorted(c[0] for c in cols)
    h = min(hues, key=lambda ref: sum(_hue_dist(ref, x) for x in hues))
    s = sorted(c[1] for c in cols)[len(cols) // 2]
    v = sorted(c[2] for c in cols)[len(cols) // 2]
    return (h, s, v, len(cols))


def colores_distintos(a, b):
    """True si dos camiones son de distinto color. La divergencia de hue
    solo cuenta con V estable (los cambios de iluminacion no son cambio
    de color). Pares grises (S bajo) se comparan por valor V."""
    ca, cb = _color_ancla(a), _color_ancla(b)
    if not ca or not cb:
        return False
    dv = abs(ca[2] - cb[2])
    if ca[1] < COLOR_S_GRIS and cb[1] < COLOR_S_GRIS:
        return dv > COLOR_DV_GRIS
    if ca[1] < COLOR_S_GRIS or cb[1] < COLOR_S_GRIS:
        return dv > COLOR_DV_GRIS
    if dv > COLOR_V_ESTABLE:
        return False
    return _hue_dist(ca[0], cb[0]) > COLOR_HUE_TOL


def _split_color_anclado(dets):
    """Un cluster con deriva de color anclada (hue de las 2 primeras
    lecturas validas): si >=3 dets seguidos divergen con V estable, el
    cluster se parte ahi (frontera dura, como el sweeper)."""
    cols = [_color_de(d) for d in dets]
    validos = [i for i, c in enumerate(cols) if c is not None]
    if len(validos) < COLOR_MIN_MUESTRAS + 3:
        return [dets]
    h0 = cols[validos[0]][0]
    h1 = cols[validos[1]][0]
    v_ancla = (cols[validos[0]][2] + cols[validos[1]][2]) / 2

    def diverge(i):
        c = cols[i]
        if c is None or c[2] < COLOR_V_MIN:
            return False
        if abs(c[2] - v_ancla) > COLOR_V_ESTABLE:
            return False
        return (_hue_dist(c[0], h0) > COLOR_HUE_TOL and
                _hue_dist(c[0], h1) > COLOR_HUE_TOL)

    cortes = []
    i = 0
    while i < len(dets):
        if not diverge(i):
            i += 1
            continue
        j = i
        while j < len(dets) and diverge(j):
            j += 1
        if j - i >= COLOR_MIN_DIVERGE and i > 0:
            cortes.append(dets[i]["ts"])
        i = max(j, i + 1)
    if not cortes:
        return [dets]
    out = []
    for d in dets:
        k = sum(1 for t in cortes if d["ts"] >= t)
        if k >= len(out):
            out.append([])
        out[k].append(d)
    return [o for o in out if o]


def leer(path, offs):
    out = []
    try:
        with open(path) as fh:
            fh.seek(offs.get(path, 0))
            out = fh.readlines()
            offs[path] = fh.tell()
    except FileNotFoundError:
        pass
    return out


def _grupos_codigo(cods):
    grupos = []
    for c in cods:
        for g in grupos:
            if ocr_codes._levenshtein(c, g[0]) <= 2:
                g.append(c)
                break
        else:
            grupos.append([c])
    return grupos


def clusters_de(recs_cam):
    dets = sorted((r for r in recs_cam if r["tipo"] == "det"),
                  key=lambda r: r["ts"])
    ocr = {r["seq"]: r for r in recs_cam if r["tipo"] == "ocr"}
    for d in dets:
        d["_ocr"] = ocr.get(d["seq"])
    clusters = []
    for d in dets:
        if clusters and d["ts"] - clusters[-1][-1]["ts"] <= GAP_CLUSTER:
            clusters[-1].append(d)
        else:
            clusters.append([d])
    out = []
    for c in clusters:
        for sub in _split_trayectoria(c):
            sub = _recortar_estacionado(sub)
            if sub:
                out.append(sub)
    return out


def _recortar_estacionado(dets):
    """Elimina el prefijo/sufijo inmóvil de un cluster (camiones
    estacionados al margen de la vía que contaminan el inicio/fin). Un
    cluster sin movimiento sostenido (>=3 saltos > UMBRAL_MOV) es
    basura de estacionado y se descarta completo."""
    if len(dets) < 2:
        return []
    cxs = [d["cx"] for d in dets]
    dxs = [abs(cxs[i + 1] - cxs[i]) for i in range(len(dets) - 1)]
    mov = [i for i, dx in enumerate(dxs) if dx > UMBRAL_MOV]
    if not mov:
        return []
    if len(dxs) >= 6 and len(mov) < MIN_MUESTRAS_MOV:
        return []
    ini = max(0, mov[0] - 2)
    fin = min(len(dets) - 1, mov[-1] + 2)
    return dets[ini:fin + 1]


def _split_trayectoria(dets):
    if not dets:
        return [dets]
    cam = dets[0]["cam"]
    best = {}
    for d in dets:
        if d["f"] not in best or d["conf"] > best[d["f"]]["conf"]:
            best[d["f"]] = d
    ds = [best[f] for f in sorted(best)]
    umbral_chico = PAD_AREA_EQ * (W / 960) ** 2
    cortes = []
    for i in range(len(ds) - 1):
        a, b = ds[i], ds[i + 1]
        dx = b["cx"] - a["cx"]
        entrada = ((cam == 1 and b["cx"] < 0.2 * W and a["cx"] > 0.8 * W) or
                   (cam == 2 and b["cx"] > 0.8 * W and a["cx"] < 0.2 * W))
        if not (entrada and abs(dx) >= 0.25 * W
                and b["area"] >= 5 * a["area"] and a["area"] < umbral_chico):
            continue
        if i + 1 < len(ds) and abs(ds[i + 1]["cx"] - b["cx"]) < 0.35 * W:
            cortes.append((a["f"] + b["f"]) // 2)
    if not cortes:
        return [dets]
    out = []
    for d in dets:
        k = sum(1 for c in cortes if d["f"] >= c)
        if k >= len(out):
            out.append([])
        out[k].append(d)
    return [o for o in out if o]


PAD_AREA_EQ = 1500.0


def fronteras_de(cluster):
    lect = sorted((d["f"], c) for d in cluster if d.get("_ocr")
                  for c, _ in d["_ocr"].get("codigos", []))
    if len(lect) < 2:
        return []
    grupos = _grupos_codigo([c for _, c in lect])
    if len(grupos) < 2:
        return []
    gmap = {}
    for _, c in lect:
        for i, g in enumerate(grupos):
            if ocr_codes._levenshtein(c, g[0]) <= 2:
                gmap[c] = i
                break
    ts_by_f = {d["f"]: d["ts"] for d in cluster}
    frs = []
    prev = lect[0]
    for f, c in lect[1:]:
        if gmap.get(c) != gmap.get(prev[1]) and f - prev[0] >= GAP_CODIGO:
            t_prev, t_cur = ts_by_f.get(prev[0]), ts_by_f.get(f)
            if t_prev is not None and t_cur is not None:
                frs.append((t_prev + t_cur) / 2)
        prev = (f, c)
    return frs


def corroborado(tsf, clusters_otra, frs_otra):
    """La frontera es real si la otra camara muestra DOS camiones
    tocandose justo ahi (uno termina, otro empieza) o una frontera de
    codigo propia en el mismo instante."""
    a = [c for c in clusters_otra
         if c and tsf - 45 <= c[-1]["ts"] <= tsf + 5]
    b = [c for c in clusters_otra
         if c and tsf - 5 <= c[0]["ts"] <= tsf + 45]
    for ca in a:
        for cb in b:
            if ca is cb:
                continue
            if ca[-1]["ts"] <= cb[0]["ts"] + 10:
                return True
    return any(abs(t - tsf) <= TOL_FRONTERA for t in frs_otra)


def aplicar_cortes(clusters, corroborar):
    out = []
    for c in clusters:
        cortes = sorted(set(round(t, 2)
                            for t in fronteras_de(c) if corroborar(t)))
        if not cortes:
            out.append(c)
            continue
        sub = []
        for d in c:
            k = sum(1 for t in cortes if d["ts"] >= t)
            if k >= len(sub):
                sub.append([])
            sub[k].append(d)
        out.extend(s for s in sub if s)
    return out


def _filtrar_direccion_cam2(clusters):
    """cam2 valida = der->izq (cx decreciente). Clusters moviendose
    izq->der son trafico de la calle contraria y se descartan."""
    out = []
    for c in clusters:
        cxs = [d["cx"] for d in sorted(c, key=lambda d: d["f"])]
        if len(cxs) >= 4:
            dxs = sorted(cxs[i + 1] - cxs[i] for i in range(len(cxs) - 1))
            med = dxs[len(dxs) // 2]
            if med > 1.5:
                continue
        out.append(c)
    return out


def _pesos_de(cluster):
    tiers = {}
    for d in cluster:
        for c, t in (d.get("_ocr") or {}).get("codigos", []):
            tiers.setdefault(c, Counter())[t] += 1
    pesos = {}
    for c, ct in tiers.items():
        p = 2.0 * ct.get("strict", 0) + 1.0 * ct.get("repaired", 0)
        if ct.get("raw", 0) >= 3:
            p += 0.5 * ct.get("raw", 0)
        if p > 0:
            pesos[c] = p
    return pesos


def _split_familia_cruzada(clusters, clusters_otra):
    """Un cluster con dos familias fuertes y sin frontera temporal (dos
    camiones pegados con lecturas intercaladas) se parte si la OTRA
    camara tiene un cluster propio con esa familia en la misma ventana."""
    fams_otra = []
    for c in clusters_otra:
        p = _pesos_de(c)
        if p:
            fam, peso = mejor_codigo_peso(p)
            if fam and peso >= 4:
                fams_otra.append((min(d["ts"] for d in c),
                                  max(d["ts"] for d in c), fam))
    out = []
    for c in clusters:
        p = _pesos_de(c)
        if not p:
            out.append(c)
            continue
        fam, pesof = mejor_codigo_peso(p)
        t0, t1 = min(d["ts"] for d in c), max(d["ts"] for d in c)
        cortes = []
        for f, peso_f in sorted(p.items(), key=lambda x: -x[1]):
            if f == fam or ocr_codes._levenshtein(f, fam) <= 2:
                continue
            if peso_f < max(2.5, 0.25 * pesof):
                continue
            if any(t0 - 30 <= t0x and t1x <= t1 + 30 and
                   ocr_codes._levenshtein(f, fam2) <= 2
                   for t0x, t1x, fam2 in fams_otra):
                cortes.append(f)
        if not cortes:
            out.append(c)
            continue
        sub = []
        resto = []
        for d in c:
            cods = [cod for cod, _ in
                    (d.get("_ocr") or {}).get("codigos", [])]
            if cods and any(ocr_codes._levenshtein(cod, f) <= 2
                            for f in cortes for cod in cods):
                sub.append(d)
            else:
                resto.append(d)
        if len(sub) >= 3 and len(resto) >= 3:
            out.append(sub)
            out.append(resto)
        else:
            out.append(c)
    return out


def camiones(recs1, recs2):
    c1 = clusters_de(recs1)
    c2 = _filtrar_direccion_cam2(clusters_de(recs2))
    for _ in range(2):
        fr1 = [t for c in c1 for t in fronteras_de(c)]
        fr2 = [t for c in c2 for t in fronteras_de(c)]
        c1 = aplicar_cortes(c1, lambda t: corroborado(t, c2, fr2))
        c2 = aplicar_cortes(c2, lambda t: corroborado(t, c1, fr1))
    c1 = _split_familia_cruzada(c1, c2)
    c2 = _split_familia_cruzada(c2, c1)
    c1 = [s for c in c1 for s in _split_color_anclado(c)]
    c2 = [s for c in c2 for s in _split_color_anclado(c)]
    return c1, c2


def _recalcular_pesos(truck):
    pesos = {}
    for c, ct in truck["tiers"].items():
        peso = 2.0 * ct.get("strict", 0) + 1.0 * ct.get("repaired", 0)
        if ct.get("raw", 0) >= 3:
            peso += 0.5 * ct.get("raw", 0)
        if peso > 0:
            pesos[c] = peso
    truck["pesos"] = pesos


def _mezclar_reocr(truck):
    for c, ct in truck.get("reocr", {}).items():
        truck["tiers"].setdefault(c, Counter()).update(ct)
    _recalcular_pesos(truck)


def truck_de(dets):
    cam = dets[0]["cam"]
    votes = Counter()
    tiers = {}
    for d in dets:
        if d.get("_ocr"):
            for c, tier in d["_ocr"].get("codigos", []):
                votes[c] += 1
                tiers.setdefault(c, Counter())[tier] += 1
    truck = {"cam": cam, "dets": dets, "votes": votes,
             "tiers": tiers,
             "ts_inicio": dets[0]["ts"], "ts_fin": dets[-1]["ts"],
             "picos": []}
    _recalcular_pesos(truck)
    return truck


def mejor_codigo_peso(pesos, tiers=None):
    """Codigo por votacion ponderada (strict 2 / repaired 1 / raw 0.5,
    raw solo si >=3 lecturas). Desempate dentro del vecindario fuzzy
    por cantidad de lecturas strict."""
    if not pesos:
        return None, 0
    unicos = list(pesos)
    vecinos = {c: pesos[c] for c in unicos}
    for i in range(len(unicos)):
        for j in range(i + 1, len(unicos)):
            if ocr_codes._levenshtein(unicos[i], unicos[j]) <= 2:
                vecinos[unicos[i]] += pesos[unicos[j]]
                vecinos[unicos[j]] += pesos[unicos[i]]
    tiers = tiers or {}
    best = max(unicos, key=lambda c: (
        vecinos[c],
        tiers.get(c, Counter()).get("strict", 0),
        pesos[c]))
    return best, vecinos[best]


def pesos_combinados(a, b):
    """Pesos de ambas camaras; +2 de bonus a codigos leidos por las dos."""
    out = {}
    tiers = {}
    for t in (a, b):
        if not t:
            continue
        for c, p in t.get("pesos", {}).items():
            out[c] = out.get(c, 0) + p
            tiers.setdefault(c, Counter()).update(t["tiers"].get(c, {}))
    if a and b:
        for c in set(a.get("pesos", {})) & set(b.get("pesos", {})):
            out[c] = out.get(c, 0) + 2.0
    return out, tiers


def es_ruido(truck):
    """Cluster sin codigo, sin puertas y con pocas detecciones: parpadeo
    de camiones estacionados, no un camion pasando."""
    return (not truck["votes"] and not truck["picos"]
            and len(truck["dets"]) < 8)


def asignar_picos(trucks, recs_cam):
    picos = [r for r in recs_cam if r["tipo"] == "pico"]
    sobra = []
    for p in picos:
        candidatos = [t for t in trucks
                      if t["ts_inicio"] - 10 <= p["ts"] <= t["ts_fin"] + 30]
        if not candidatos:
            sobra.append(p)
            continue
        # un pico pertenece al camion del MISMO run cuyo span lo contiene;
        # desempate por centro mas cercano (las puertas vienen al final)
        mismo_run = [t for t in candidatos
                     if any(d.get("run") == p.get("run")
                            for d in t["dets"])]
        en_span = [t for t in mismo_run
                   if t["ts_inicio"] - 3 <= p["ts"] <= t["ts_fin"] + 5]
        pool = en_span or mismo_run or candidatos
        # las puertas van al FINAL de la pasada: gana el camion cuyo
        # fin de span esta mas cerca del pico
        t = min(pool, key=lambda t: abs(p["ts"] - t["ts_fin"]))
        t["picos"].append(p)
    sobra.sort(key=lambda p: p["ts"])
    grupos = []
    for p in sobra:
        if grupos and p["ts"] - grupos[-1][-1]["ts"] <= GAP_CLUSTER:
            grupos[-1].append(p)
        else:
            grupos.append([p])
    for g in grupos:
        trucks.append({"cam": g[0]["cam"], "dets": [], "votes": Counter(),
                       "tiers": {}, "pesos": {},
                       "ts_inicio": g[0]["ts"], "ts_fin": g[-1]["ts"],
                       "picos": g})
    for t in trucks:
        if t["picos"]:
            t["ts_fin"] = max(t["ts_fin"], max(p["ts"] for p in t["picos"]))
    return trucks


def reocr_necesario(truck):
    grupos = _grupos_codigo(list(truck["votes"].elements()))
    if len(grupos) < 2:
        return False
    scores = sorted((sum(truck["votes"][c] for c in g) for g in grupos),
                    reverse=True)
    return scores[0] - scores[1] < MARGEN_AMBIGUO


def aplicar_reocr(trucks, cache_path, server):
    cache = {}
    try:
        with open(cache_path) as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        cache = {}
    tocado = False
    for t in trucks:
        if not reocr_necesario(t):
            continue
        cands = [d for d in t["dets"]
                 if d.get("crop") and not d.get("_ocr")
                 and d["crop"] not in cache
                 and os.path.exists(d["crop"])]
        cands.sort(key=lambda d: -d["area"])
        for d in cands[:3]:
            try:
                r = requests.post(server + "/ocr",
                                  files={"file": open(d["crop"], "rb")},
                                  timeout=180)
                r.raise_for_status()
                cods = ocr_codes.extraer_codigos(
                    ocr_codes.limpiar(r.json().get("text", "")))
            except requests.RequestException:
                cods = []
            cache[d["crop"]] = {"codigos": [[c, t] for c, t, _ in cods]}
            tocado = True
    if tocado:
        claves = sorted(cache)[-2000:]
        try:
            with open(cache_path, "w") as fh:
                json.dump({k: cache[k] for k in claves}, fh)
        except OSError:
            pass
    for t in trucks:
        for d in t["dets"]:
            e = cache.get(d.get("crop"))
            if e:
                for c, tier in e["codigos"]:
                    t["votes"][c] += 1
                    t.setdefault("reocr", {}).setdefault(
                        c, Counter())[tier] += 1


def emparejar(t1, t2):
    libres1 = list(range(len(t1)))
    libres2 = list(range(len(t2)))
    pares = []
    for i in list(libres1):
        fam = t1[i].get("familia")
        if not fam:
            continue
        mejor = None
        for j in libres2:
            fam2 = t2[j].get("familia")
            if not fam2:
                continue
            if ocr_codes._levenshtein(fam, fam2) > 2:
                continue
            if colores_distintos(t1[i], t2[j]):
                continue
            dt = abs(t1[i]["ts_inicio"] - t2[j]["ts_inicio"])
            if dt <= TOL_CODIGO and (mejor is None or dt < mejor[0]):
                mejor = (dt, j)
        if mejor:
            pares.append((i, mejor[1]))
            libres1.remove(i)
            libres2.remove(mejor[1])
    i = j = 0
    while i < len(libres1) and j < len(libres2):
        a, b = t1[libres1[i]], t2[libres2[j]]
        dt = abs(a["ts_inicio"] - b["ts_inicio"])
        # no emparejar por secuencia cuando ambas camaras leyeron
        # familias distintas: son camiones diferentes (o lecturas tan
        # dispares que es mejor mostrarlas por separado que cruzadas)
        fa, fb = a.get("familia"), b.get("familia")
        conflicto = (fa and fb and ocr_codes._levenshtein(fa, fb) > 2) \
            or colores_distintos(a, b)
        if conflicto:
            if a["ts_inicio"] < b["ts_inicio"]:
                i += 1
            else:
                j += 1
            continue

        def cerca(truck, lista, k):
            return any(abs(truck["ts_inicio"] - o["ts_inicio"]) <= CERCANIA
                       for o in lista if o is not truck)
        tol = TOL_CONGESTION if (cerca(a, t1, libres1[i]) or
                                 cerca(b, t2, libres2[j])) else TOL_AISLADO
        if dt <= tol:
            pares.append((libres1[i], libres2[j]))
            i += 1
            j += 1
        elif a["ts_inicio"] < b["ts_inicio"]:
            i += 1
        else:
            j += 1
    return pares


def sello_de(truck):
    if not truck["picos"]:
        return {"veredicto": "sin datos", "cls": None, "conf": None}
    por_run = {}
    for p in truck["picos"]:
        por_run.setdefault(p.get("run"), []).append(p)
    mejor = max(por_run.values(),
                key=lambda ps: sum(x["n_sellos"] for x in ps))
    frames_res = [{"frame": p["f"], "n_sellos": p["n_sellos"],
                   "cls": p.get("cls"), "conf": p.get("conf")}
                  for p in mejor]
    v = _veredicto_frames(frames_res)
    return {"veredicto": v["veredicto"], "cls": v["cls"],
            "conf": v["conf"]}


def mejor_foto(truck):
    """Foto del pico del run con mas lecturas de la familia del camion
    (desempate por ts: la pasada mas reciente)."""
    if not truck["picos"]:
        return None
    fam = truck.get("familia")

    def score(p):
        run = p.get("run")
        if not fam:
            return 0
        return sum(1 for d in truck["dets"]
                   if d.get("run") == run and d.get("_ocr") and
                   any(ocr_codes._levenshtein(c, fam) <= 2
                       for c, _ in d["_ocr"].get("codigos", [])))
    best = max(truck["picos"], key=lambda p: (score(p), p["ts"]))
    return (best["n_sellos"], best.get("foto"), best.get("sello_crop"))


def mejor_crop_codigo(truck, f4k_by_f, salida):
    dets_crop = [d for d in truck["dets"] if d.get("crop")
                 and os.path.exists(d["crop"])]
    if dets_crop:
        return max(dets_crop, key=lambda d: d["area"])["crop"]
    for d in sorted(truck["dets"], key=lambda d: -d["area"]):
        path4k = f4k_by_f.get(d["f"])
        if not path4k or not os.path.exists(path4k):
            continue
        img = cv2.imread(path4k)
        if img is None:
            continue
        x1, y1, x2, y2 = d["bbox"]
        bw, bh = x2 - x1, y2 - y1
        px, py = int(bw * PAD_X), int(bh * PAD_Y)
        cx1, cy1 = max(0, int(x1 - px)), max(0, int(y1 - py))
        cx2, cy2 = min(W, int(x2 + px)), min(H, int(y2 + py))
        crop = img[cy1:cy2, cx1:cx2]
        if crop.shape[0] < 60 or crop.shape[1] < 60:
            continue
        os.makedirs(os.path.join(salida, "fotos"), exist_ok=True)
        path = os.path.join(salida, "fotos",
                            f"codigo_cam{d['cam']}_f{d['f']:06d}.jpg")
        ok, buf = cv2.imencode(".jpg", crop,
                               [cv2.IMWRITE_JPEG_QUALITY, 90])
        if ok:
            with open(path, "wb") as fh:
                fh.write(buf.tobytes())
            return f"fotos/{os.path.basename(path)}"
    return None


def hora(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _cargar_ids(salida):
    try:
        with open(os.path.join(salida, "ids.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _guardar_ids(salida, ids):
    claves = sorted(ids, key=lambda k: float(k.rsplit("_", 1)[1]))[-400:]
    with open(os.path.join(salida, "ids.json"), "w") as fh:
        json.dump({k: ids[k] for k in claves}, fh)


def _mediana_ponderada(pares):
    orden = sorted(pares, key=lambda x: x[0])
    total = sum(w for _, w in orden)
    if not total:
        return 0.0
    acum = 0.0
    for v, w in orden:
        acum += w
        if acum >= total / 2:
            return v
    return orden[-1][0]


def _metrica_truck(t, runinfo):
    if not t or not t["dets"]:
        return None
    infos = {}
    for d in t["dets"]:
        k = (t["cam"], d.get("run"))
        if k in runinfo and k not in infos:
            infos[k] = runinfo[k]
    pares = [(i["vel_px_s"], i["n_muestras"]) for i in infos.values()]
    vel = round(_mediana_ponderada(pares), 1) if pares else None
    laps = [i["lap_med"] for i in infos.values() if i.get("lap_med")]
    nit = round(sorted(laps)[len(laps) // 2], 1) if laps else None
    total = sum(1 for d in t["dets"] if d.get("_ocr"))
    val = sum(1 for d in t["dets"]
              if d.get("_ocr") and d["_ocr"].get("codigos"))
    lect = f"{val}/{total}" if total else None
    confs = [d["conf"] for d in t["dets"]]
    conf = round(sum(confs) / len(confs), 2) if confs else None
    return {"vel_px_s": vel, "nitidez": nit, "lecturas": lect,
            "conf": conf}


def _fusionar_fragmentos(trucks):
    """Camiones con <3 lecturas validas son fragmentos de un split:
    se fusionan al camion vecino de la misma familia (o el mas cercano
    en el tiempo), transfiriendo dets, picos y votos."""
    out = list(trucks)
    cambiado = True
    while cambiado:
        cambiado = False
        for t in list(out):
            lect = sum(1 for d in t["dets"]
                       if d.get("_ocr") and d["_ocr"].get("codigos"))
            if lect >= 3 or not t["dets"]:
                continue
            # con evidencia fisica sustancial (>=10 dets Y puertas) no es
            # fragmento aunque el OCR haya fallado
            if len(t["dets"]) >= 10 and t["picos"]:
                continue
            fam, _ = mejor_codigo_peso(t["pesos"], t["tiers"])
            cands = [o for o in out
                     if o is not t and o["cam"] == t["cam"] and o["dets"]]
            if not cands:
                continue
            pool = cands
            if fam:
                misma = [o for o in cands
                         if o.get("familia") and
                         ocr_codes._levenshtein(fam, o["familia"]) <= 2]
                if misma:
                    pool = misma
            mismo_color = [o for o in pool
                           if not colores_distintos(t, o)]
            if mismo_color:
                pool = mismo_color
            o = min(pool, key=lambda o: abs(o["ts_inicio"] -
                                            t["ts_inicio"]))
            o["dets"].extend(t["dets"])
            o["picos"].extend(t["picos"])
            for c, ct in t["tiers"].items():
                o["tiers"].setdefault(c, Counter()).update(ct)
            _recalcular_pesos(o)
            o["ts_inicio"] = min(o["ts_inicio"], t["ts_inicio"])
            o["ts_fin"] = max(o["ts_fin"], t["ts_fin"])
            out.remove(t)
            cambiado = True
            break
    # pasada 2: mismo codigo (lev<=2) y <15s en la misma camara =
    # el mismo camion partido por un split
    cambiado = True
    while cambiado:
        cambiado = False
        for t in list(out):
            if t not in out:
                continue
            fam = t.get("familia")
            if not fam:
                continue
            for o in out:
                if o is t or o["cam"] != t["cam"]:
                    continue
                fam2 = o.get("familia")
                if not fam2:
                    continue
                if ocr_codes._levenshtein(fam, fam2) <= 2 and \
                        abs(o["ts_inicio"] - t["ts_inicio"]) <= 90 and \
                        not colores_distintos(t, o):
                    t["dets"].extend(o["dets"])
                    t["picos"].extend(o["picos"])
                    for c, ct in o["tiers"].items():
                        t["tiers"].setdefault(c, Counter()).update(ct)
                    _recalcular_pesos(t)
                    t["ts_inicio"] = min(t["ts_inicio"], o["ts_inicio"])
                    t["ts_fin"] = max(t["ts_fin"], o["ts_fin"])
                    out.remove(o)
                    cambiado = True
                    break
            if cambiado:
                break
    return out


def construir(recs1, recs2, args):
    ahora = time.time()
    c1, c2 = camiones(recs1, recs2)
    t1 = [truck_de(dets) for dets in c1 if dets]
    t2 = [truck_de(dets) for dets in c2 if dets]
    t1 = asignar_picos(t1, recs1)
    t2 = asignar_picos(t2, recs2)
    for t in t1 + t2:
        t["ts_fin"] = max(t["ts_fin"], *(p["ts"] for p in t["picos"])) \
            if t["picos"] else t["ts_fin"]
    t1 = [t for t in t1 if not es_ruido(t)]
    t2 = [t for t in t2 if not es_ruido(t)]
    aplicar_reocr(t1 + t2, os.path.join(args.salida, "reocr.json"),
                  args.server)
    for t in t1 + t2:
        _mezclar_reocr(t)
        t["familia"], _ = mejor_codigo_peso(t["pesos"], t["tiers"])
    t1 = _fusionar_fragmentos(t1)
    t2 = _fusionar_fragmentos(t2)
    for t in t1 + t2:
        t["color"] = _color_ancla(t)
    cerradas1 = [t for t in t1 if ahora - t["ts_fin"] >= CIERRE]
    cerradas2 = [t for t in t2 if ahora - t["ts_fin"] >= CIERRE]

    os.makedirs(args.salida, exist_ok=True)
    for nombre in ("fotos_b3", "crops_b3"):
        enlace = os.path.join(args.salida, nombre)
        if not os.path.islink(enlace) and not os.path.exists(enlace):
            try:
                os.symlink(os.path.join("..", "..", nombre), enlace)
            except OSError:
                pass

    f4k_by_f = {r["f"]: r["path"] for r in recs1 + recs2
                if r["tipo"] == "frame4k"}
    runinfo = {}
    for r in recs1 + recs2:
        if r["tipo"] == "run_info":
            runinfo[(r["cam"], r["run"])] = r

    pares = emparejar(cerradas1, cerradas2)
    usados1 = {i for i, _ in pares}
    usados2 = {j for _, j in pares}
    filas = [(cerradas1[i], cerradas2[j], None) for i, j in pares]
    filas += [(cerradas1[i], None, None) for i in range(len(cerradas1))
              if i not in usados1]
    filas += [(None, cerradas2[j], None) for j in range(len(cerradas2))
              if j not in usados2]
    # red final: nunca dos colores distintos en la misma lectura
    expandidas = []
    for a, b, motivo in filas:
        if a and b and colores_distintos(a, b):
            expandidas.append((a, None, "color"))
            expandidas.append((None, b, "color"))
        else:
            expandidas.append((a, b, motivo))
    filas = expandidas
    filas.sort(key=lambda x: (x[0] or x[1])["ts_inicio"])

    ids = _cargar_ids(args.salida)
    prox = max(ids.values(), default=0) + 1
    ids_new = {}
    for a, b, motivo in filas:
        ini = (a or b)["ts_inicio"]
        cam = (a or b)["cam"]
        clave = f"cam{cam}_{ini:.2f}"
        cid = ids.get(clave)
        if cid is None:
            cid = prox
            prox += 1
        ids_new[clave] = cid
    _guardar_ids(args.salida, ids_new)

    secciones = []
    registro = []
    for a, b, motivo in reversed(filas):
        ini = (a or b)["ts_inicio"]
        cam = (a or b)["cam"]
        cid = ids_new[f"cam{cam}_{ini:.2f}"]
        v1 = a["votes"] if a else Counter()
        v2 = b["votes"] if b else Counter()
        cod1, n1 = mejor_codigo_peso(a["pesos"], a["tiers"]) if a \
            else (None, 0)
        cod2, n2 = mejor_codigo_peso(b["pesos"], b["tiers"]) if b \
            else (None, 0)
        pf, tiers_f = pesos_combinados(a, b)
        codf, nf = mejor_codigo_peso(pf, tiers_f)
        disc = []
        if cod1 and cod2 and ocr_codes._levenshtein(cod1, cod2) > 2:
            disc.append("DISCREPANCIA")
        if cod1 and not cod2 and motivo != "color":
            disc.append("cam2 no leyó código")
        if cod2 and not cod1 and motivo != "color":
            disc.append("cam1 no leyó código")
        if not cod1 and not cod2:
            disc.append("CÓDIGO NO LEGIBLE")
        if motivo == "color":
            disc.append("COLOR DISTINTO")
        s1 = sello_de(a) if a else None
        s2 = sello_de(b) if b else None
        sf = sello_fusion(s1 or {"cls": None, "conf": None},
                          s2 or {"cls": None, "conf": None})
        clase = {"CON SELLO": "con", "SIN SELLO": "sin",
                 "DUDA": "duda"}[sf["veredicto"]]
        partes = []
        met1 = _metrica_truck(a, runinfo)
        met2 = _metrica_truck(b, runinfo)

        def swatch(t):
            c = t.get("color") if t else None
            if not c:
                return ""
            h, s, v, n = c
            bg = f"hsl({h * 360:.0f},{min(s * 100, 100):.0f}%," \
                 f"{min(v * 100, 85):.0f}%)"
            return (f"<span class='swatch' style='background:{bg}' "
                    f"title='hsv {h:.2f}/{s:.2f}/{v:.2f} (n={n})'></span>")

        for lado, t, codc, met in (("cam1", a, cod1, met1),
                                   ("cam2", b, cod2, met2)):
            if not t:
                partes.append(f"<div><b>{lado}</b>: sin datos</div>")
                continue
            html_p = []
            sw = swatch(t)
            if sw:
                html_p.append(sw)
            mejor = mejor_foto(t)
            if mejor:
                _, f, fc = mejor
                fig = (f"<figure><a href='{f}' target='_blank'>"
                       f"<img src='{f}'></a>"
                       f"<figcaption>{lado} {os.path.basename(f)} "
                       f"({mejor[0]} sellos)</figcaption></figure>")
                if fc and os.path.exists(fc):
                    fig += (f"<figure><a href='{fc}' target='_blank'>"
                            f"<img class='crop' src='{fc}'></a>"
                            f"<figcaption>recorte sello (click = real)"
                            f"</figcaption></figure>")
                html_p.append(fig)
            elif t["dets"]:
                best = max(t["dets"], key=lambda d: d["area"])
                path4k = f4k_by_f.get(best["f"])
                if path4k and os.path.exists(path4k):
                    html_p.append(
                        f"<figure><a href='{path4k}' target='_blank'>"
                        f"<img src='{path4k}'></a>"
                        f"<figcaption>{lado} solo código "
                        f"(sin sellos en esta cámara)</figcaption></figure>")
            crop_cod = mejor_crop_codigo(t, f4k_by_f, args.salida)
            if crop_cod:
                html_p.append(
                    f"<figure><a href='{crop_cod}' target='_blank'>"
                    f"<img class='crop' src='{crop_cod}'></a>"
                    f"<figcaption>{lado} mejor crop código "
                    f"(click = real)</figcaption></figure>")
            if met:
                html_p.append(
                    f"<div style='color:#9aa4b2;font-size:12px;"
                    f"margin-top:4px'>vel {met['vel_px_s']} px/s · "
                    f"nitidez {met['nitidez']} · "
                    f"lecturas {met['lecturas']} · conf {met['conf']}"
                    f"</div>")
            partes.append(f"<div><b>{lado}</b> {''.join(html_p) or '—'}"
                          f"</div>")
        disc_html = "".join(f"<span class='badge warn'>{esc(x)}</span> "
                            for x in disc) or \
            "<span class='badge ok'>OK</span>"
        secciones.append(
            f"<section><h2>#{cid} · {hora(ini)} · "
            f"<span class='code'>{esc(codf) if codf else 'NO LEGIBLE'}</span>"
            f" <span class='badge {clase}'>{esc(sf['veredicto'])}</span></h2>"
            f"<p>{disc_html} cam1={esc(cod1) if cod1 else '—'}({n1}) · "
            f"cam2={esc(cod2) if cod2 else '—'}({n2}) · "
            f"sello cam1={esc(s1['veredicto']) if s1 else '—'} · "
            f"sello cam2={esc(s2['veredicto']) if s2 else '—'}</p>"
            f"<div class='cam'>{''.join(partes)}</div></section>")
        registro.append({"id": cid, "ts_inicio": round(ini),
                         "hora": hora(ini), "codigo": codf, "disc": disc,
                         "sello": sf["veredicto"],
                         "cam1_codigo": cod1, "cam2_codigo": cod2,
                         "cam1_sello": s1["veredicto"] if s1 else None,
                         "cam2_sello": s2["veredicto"] if s2 else None,
                         "cam1_vel_px_s": met1["vel_px_s"] if met1 else None,
                         "cam2_vel_px_s": met2["vel_px_s"] if met2 else None,
                         "cam1_nitidez": met1["nitidez"] if met1 else None,
                         "cam2_nitidez": met2["nitidez"] if met2 else None,
                         "cam1_lecturas": met1["lecturas"] if met1 else None,
                         "cam2_lecturas": met2["lecturas"] if met2 else None,
                          "cam1_conf": met1["conf"] if met1 else None,
                          "cam2_conf": met2["conf"] if met2 else None,
                          "cam1_color": a["color"] if a else None,
                          "cam2_color": b["color"] if b else None})

    with open(os.path.join(args.salida, "containers.json"), "w") as fh:
        json.dump(registro, fh, indent=1, ensure_ascii=False)
    POR_PAGINA = 5
    paginas = [secciones[i:i + POR_PAGINA]
               for i in range(0, len(secciones), POR_PAGINA)] or [[]]
    paginas_html = "".join(
        f"<article class='pagina'>{''.join(p)}</article>" for p in paginas)
    index = f"""<!DOCTYPE html><html lang='es'><head><meta charset='utf-8'>
<title>Contenedores EN VIVO b3</title><style>{CSS}
.pagina{{display:none}}
.pagina.activa{{display:block}}
.pager{{display:flex;gap:10px;align-items:center;margin:14px 0 22px}}
.pager button{{background:#2e3340;color:#dfe3e8;border:1px solid #3c4352;
 padding:6px 16px;border-radius:6px;cursor:pointer;font-size:14px}}
.pager button:disabled{{opacity:.4;cursor:default}}
.pager .info{{color:#9aa4b2;font-size:14px}}
.swatch{{display:inline-block;width:14px;height:14px;border-radius:50%;
 border:1px solid #3c4352;margin-right:8px;vertical-align:middle}}
</style></head><body>
<h1>Contenedores — EN VIVO (alt_batch3)</h1>
<p>{len(registro)} contenedores cerrados · actualizado
{time.strftime('%H:%M:%S')}</p>
<div class='pager'><button id='p-ant'>‹ Anterior</button>
<span class='info' id='p-info'></span>
<button id='p-sig'>Siguiente ›</button></div>
{paginas_html}
<div class='pager'><button id='p-ant2'>‹ Anterior</button>
<span class='info' id='p-info2'></span>
<button id='p-sig2'>Siguiente ›</button></div>
<script>
var paginas = document.querySelectorAll('.pagina');
var total = paginas.length;
var actual = 1;
function mostrar(p) {{
  p = Math.min(Math.max(1, p), total);
  paginas.forEach(function(el, i) {{ el.classList.toggle('activa', i + 1 === p); }});
  actual = p;
  var info = 'Página ' + p + ' de ' + total;
  document.getElementById('p-info').textContent = info;
  document.getElementById('p-info2').textContent = info;
  document.getElementById('p-ant').disabled = p <= 1;
  document.getElementById('p-ant2').disabled = p <= 1;
  document.getElementById('p-sig').disabled = p >= total;
  document.getElementById('p-sig2').disabled = p >= total;
  history.replaceState(null, '', '#p=' + p);
}}
document.getElementById('p-ant').addEventListener('click', function() {{ mostrar(actual - 1); }});
document.getElementById('p-ant2').addEventListener('click', function() {{ mostrar(actual - 1); }});
document.getElementById('p-sig').addEventListener('click', function() {{ mostrar(actual + 1); }});
document.getElementById('p-sig2').addEventListener('click', function() {{ mostrar(actual + 1); }});
var m = location.hash.match(/p=(\\d+)/);
mostrar(m ? parseInt(m[1], 10) : 1);
setTimeout(function() {{ location.reload(); }}, 10000);
</script></body></html>"""
    with open(os.path.join(args.salida, "index.html"), "w") as fh:
        fh.write(index)
    print(f"[informe] {len(registro)} contenedores "
          f"({time.strftime('%H:%M:%S')})", flush=True)


def purgar_imagenes(ahora):
    lim = ahora - RETENCION_DIAS * 86400
    for base in ("fotos_b3", "crops_b3"):
        for root, _, files in os.walk(base):
            for fn in files:
                p = os.path.join(root, fn)
                try:
                    if os.path.getmtime(p) < lim:
                        os.remove(p)
                except OSError:
                    pass


def main():
    sys.stdout.reconfigure(line_buffering=True)
    p = argparse.ArgumentParser()
    p.add_argument("--raw-cam1", default="raw/cam1.jsonl")
    p.add_argument("--raw-cam2", default="raw/cam2.jsonl")
    p.add_argument("--salida", default="informes/b3")
    p.add_argument("--server", default=SERVER)
    p.add_argument("--intervalo", type=float, default=5.0)
    args = p.parse_args()

    recs1, recs2 = [], []
    offs = {}
    ultimo = 0.0
    ultima_purga = time.time()
    while True:
        try:
            for ln in leer(args.raw_cam1, offs):
                recs1.append(json.loads(ln))
            for ln in leer(args.raw_cam2, offs):
                recs2.append(json.loads(ln))
            ahora = time.time()
            if ahora - ultimo >= args.intervalo:
                construir(recs1, recs2, args)
                ultimo = ahora
            if ahora - ultima_purga >= 3600:
                purgar_imagenes(ahora)
                ultima_purga = ahora
            time.sleep(1.0)
        except KeyboardInterrupt:
            print("shutdown", flush=True)
            break


if __name__ == "__main__":
    main()
