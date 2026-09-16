#!/usr/bin/env python3
"""Conciliador EN VIVO: consume los JSONL de eventos de ambas camaras y
regenera el informe por contenedor a medida que llegan datos.

Identidad por ORDEN FISICO + codigo (no por silencio):

- Por camara, un "camion abierto" a la vez (zona de una via).
- Evento con puertas (>=10 sellos) se anexa HACIA ATRAS al camion abierto
  si gap <= VENTANA_ATRAS (15s, del dato: gap codigo->puertas p99 ~5s) y
  la familia de codigo calza (o alguna de las partes no tiene codigo).
- Evento solo-codigo: misma familia extiende; familia distinta CIERRA el
  camion abierto (sus puertas no se vieron) y abre uno nuevo.
- Timeout: camion abierto sin actividad por >20s se cierra como
  "sin puertas".
- Asociacion cam1<->cam2: primero por codigo (fuzzy<=2, |dt|<=60s),
  luego alineacion de secuencia monótona con ventana inteligente
  (aislado: 10s; congestion: 40s).
- El informe muestra solo camiones cerrados.

Uso: .venv/bin/python conciliar_vivo.py --eventos-cam1 eventos/cam1.jsonl \
       --eventos-cam2 eventos/cam2.jsonl [--salida informes/containers]
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter

import ocr_codes
from conciliar import CSS, esc, mejor_codigo, sello_fusion

VENTANA_ATRAS = 15.0   # gap maximo codigo->puertas del mismo camion
GAP_CODIGO = 8         # frames minimos entre lecturas de codigo distinto
TIMEOUT_ABIERTO = 20.0  # sin actividad -> cerrar como "sin puertas"
TOL_CODIGO = 60.0      # sanidad temporal del match por codigo
TOL_AISLADO = 10.0     # ventana temporal sin congestion
TOL_CONGESTION = 40.0  # ventana temporal con camiones pegados
CERCANIA = 15.0        # eventos mas cercanos que esto = congestion
MIN_SELLOS = 10


def familia_de(ev):
    c = Counter(ev.get("codigos_votos", {}))
    cod, _ = mejor_codigo(c)
    return cod


def tiene_puertas(ev):
    return ev.get("n_sellos", 0) >= MIN_SELLOS


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


def dividir_evento(ev):
    """Si un evento puentea dos camiones (dos codigos distintos con
    frontera temporal >= 8 frames), lo parte en sub-eventos."""
    lect = sorted(ev.get("lecturas", []), key=lambda x: x[0])
    if len(lect) < 2:
        return [ev]
    grupos = _grupos_codigo([c for _, c in lect])
    if len(grupos) < 2:
        return [ev]
    gmap = {}
    for _, c in lect:
        for i, g in enumerate(grupos):
            if ocr_codes._levenshtein(c, g[0]) <= 2:
                gmap[c] = i
                break
    cortes = []
    prev = lect[0]
    for f, c in lect[1:]:
        if gmap.get(c) != gmap.get(prev[1]) and f - prev[0] >= GAP_CODIGO:
            cortes.append((prev[0] + f) // 2)
        prev = (f, c)
    if not cortes:
        return [ev]
    rangos = []
    a = None
    for corte in sorted(set(cortes)):
        rangos.append((a, corte))
        a = corte
    rangos.append((a, None))
    f0 = ev["frame_inicio"]
    f1 = max(ev["frame_fin"], f0 + 1)
    span = f1 - f0
    dur = ev["ts_fin"] - ev["ts_inicio"]
    out = []
    for lo, hi in rangos:
        lo_f = f0 if lo is None else lo
        hi_f = f1 if hi is None else hi
        sub = dict(ev)
        sub["ts_inicio"] = ev["ts_inicio"] + dur * (lo_f - f0) / span
        sub["ts_fin"] = ev["ts_inicio"] + dur * (hi_f - f0) / span
        sub["frame_inicio"] = lo_f
        sub["frame_fin"] = hi_f

        def en(f):
            return (lo is None or f >= lo) and (hi is None or f < hi)

        sub["cls3"] = [x for x in ev["cls3"] if en(x[0])]
        sub["lecturas"] = [[f, c] for f, c in ev["lecturas"] if en(f)]
        tops, fotos, fcrops = [], [], []
        fts = ev.get("fotos", [])
        fcs = ev.get("foto_crops", []) or [None] * len(fts)
        for i, t in enumerate(ev.get("sellos_tops", [])):
            if en(t["frame"]):
                tops.append(t)
                if i < len(fts):
                    fotos.append(fts[i])
                    fcrops.append(fcs[i] if i < len(fcs) else None)
        sub["sellos_tops"] = tops
        sub["fotos"] = fotos
        sub["foto_crops"] = fcrops
        crops = []
        for c in ev.get("crops", []):
            m = re.search(r"_f(\d+)_", os.path.basename(c))
            if m and en(int(m.group(1))):
                crops.append(c)
        sub["crops"] = crops
        sub["n_sellos"] = round(ev["n_sellos"] * (hi_f - lo_f) / span)
        sub["n_cls3"] = len(sub["cls3"])
        sub["codigos_votos"] = dict(Counter(c for _, c in sub["lecturas"]))
        if len(tops) >= 2:
            sub["sellos"] = dict(ev["sellos"])
        else:
            sub["sellos"] = {"veredicto": "sin datos", "cls": None,
                             "conf": None, "duda": True}
        out.append(sub)
    return out


def camiones_camara(evs, ahora):
    """Maquina de estados por camara. Devuelve camiones cerrados."""
    out = []
    abierto = None
    cerrados = []

    def cerrar(final):
        nonlocal abierto
        if abierto is not None:
            out.append(final if final else abierto)
            cerrados.append(out[-1])
            del cerrados[:-8]
        abierto = None

    def _calza(t, fam):
        return (not fam or not t["familia"] or
                ocr_codes._levenshtein(fam, t["familia"]) <= 2)

    def _buscar_cerrado(ev, fam):
        """Camion cerrado reciente SIN puertas (para anexion de puertas)."""
        for t in reversed(cerrados):
            if t.get("puertas"):
                continue
            if ev["ts_inicio"] - t["ts_ultimo"] <= VENTANA_ATRAS and \
                    _calza(t, fam):
                return t
        return None

    def _buscar_cola(ev):
        """Camion cerrado mas reciente (para la cola residual)."""
        for t in reversed(cerrados):
            if ev["ts_inicio"] - t["ts_ultimo"] <= VENTANA_ATRAS:
                return t
        return None

    def procesar(ev):
        nonlocal abierto
        fam = familia_de(ev)
        puertas = tiene_puertas(ev)
        if puertas and abierto is None:
            target = _buscar_cerrado(ev, fam)
            if target is not None:
                target["eventos"].append(ev)
                target["ts_fin"] = ev["ts_fin"]
                target["puertas"] = True
            else:
                out.append(_nuevo(ev, fam, puertas=True))
            return
        if abierto is None:
            if not puertas and not fam:
                cola = _buscar_cola(ev)
                if cola is not None:
                    cola["eventos"].append(ev)
                    cola["ts_fin"] = ev["ts_fin"]
                    return
            abierto = _nuevo(ev, fam, puertas=False)
            return
        gap = ev["ts_inicio"] - abierto["ts_ultimo"]
        if puertas:
            if gap <= VENTANA_ATRAS and _calza(abierto, fam):
                abierto["eventos"].append(ev)
                abierto["ts_fin"] = ev["ts_fin"]
                abierto["puertas"] = True
                cerrar(abierto)
            else:
                target = _buscar_cerrado(ev, fam)
                if target is not None:
                    target["eventos"].append(ev)
                    target["ts_fin"] = ev["ts_fin"]
                    target["puertas"] = True
                else:
                    cerrar(None)
                    out.append(_nuevo(ev, fam, puertas=True))
        else:
            misma = _calza(abierto, fam)
            if misma and gap <= TIMEOUT_ABIERTO:
                abierto["eventos"].append(ev)
                abierto["ts_ultimo"] = max(abierto["ts_ultimo"],
                                           ev["ts_fin"])
                if fam and not abierto["familia"]:
                    abierto["familia"] = fam
            else:
                cerrar(None)
                abierto = _nuevo(ev, fam, puertas=False)

    for ev in evs:
        for sub in dividir_evento(ev):
            procesar(sub)
    if abierto is not None and ahora - abierto["ts_ultimo"] > TIMEOUT_ABIERTO:
        cerrar(None)
    return out


def _nuevo(ev, fam, puertas):
    return {"familia": fam, "eventos": [ev],
            "ts_inicio": ev["ts_inicio"], "ts_ultimo": ev["ts_fin"],
            "ts_fin": ev["ts_fin"], "puertas": puertas}


def votos_de(truck):
    c = Counter()
    for ev in truck["eventos"]:
        c.update(ev.get("codigos_votos", {}))
    return c


def sello_de(truck):
    best = None
    for ev in truck["eventos"]:
        s = ev["sellos"]
        if s["veredicto"] == "sin datos":
            continue
        n = sum(t["n_sellos"] for t in ev.get("sellos_tops", []))
        if best is None or n > best[0]:
            best = (n, s)
    return best[1] if best else {"veredicto": "sin datos", "cls": None,
                                 "conf": None}


def fotos_de(truck):
    fotos = []
    foto_crops = {}
    crops = []
    for ev in truck["eventos"]:
        fcrops = ev.get("foto_crops", [])
        for i, f in enumerate(ev["fotos"]):
            if f not in fotos:
                fotos.append(f)
                if i < len(fcrops) and fcrops[i]:
                    foto_crops[f] = fcrops[i]
        for c in ev.get("crops", []):
            if c not in crops:
                crops.append(c)
    return fotos, foto_crops, crops


def emparejar(t1, t2):
    """Codigo primero, luego alineacion de secuencia con ventana
    inteligente (monotona)."""
    libres1 = list(range(len(t1)))
    libres2 = list(range(len(t2)))
    pares = []

    # pasada 1: match por codigo (fuzzy <= 2, |dt| <= TOL_CODIGO)
    for i in list(libres1):
        fam = t1[i]["familia"]
        if not fam:
            continue
        mejor = None
        for j in libres2:
            fam2 = t2[j]["familia"]
            if not fam2:
                continue
            if ocr_codes._levenshtein(fam, fam2) > 2:
                continue
            dt = abs(t1[i]["ts_inicio"] - t2[j]["ts_inicio"])
            if dt <= TOL_CODIGO and (mejor is None or dt < mejor[0]):
                mejor = (dt, j)
        if mejor:
            pares.append((i, mejor[1]))
            libres1.remove(i)
            libres2.remove(mejor[1])

    # pasada 2: alineacion monotona sobre los restantes
    i = j = 0
    while i < len(libres1) and j < len(libres2):
        a, b = t1[libres1[i]], t2[libres2[j]]
        dt = abs(a["ts_inicio"] - b["ts_inicio"])
        # congestion: hay otros camiones cerca?
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


def mejor_foto(truck):
    """Foto del frame con mas sellos del contenedor + su crop de sello."""
    best = None
    best_n = -1
    for ev in truck["eventos"]:
        tops = ev.get("sellos_tops", [])
        fcrops = ev.get("foto_crops", [])
        for i, top in enumerate(tops):
            if top.get("n_sellos", 0) > best_n:
                best_n = top["n_sellos"]
                if i < len(ev["fotos"]):
                    foto = ev["fotos"][i]
                    crop = fcrops[i] if i < len(fcrops) else None
                    best = (foto, crop)
    return best


def construir(evs1, evs2, args):
    evs1 = sorted(evs1, key=lambda e: e["ts_inicio"])
    evs2 = sorted(evs2, key=lambda e: e["ts_inicio"])
    ahora = time.time()
    t1 = camiones_camara(evs1, ahora)
    t2 = camiones_camara(evs2, ahora)

    os.makedirs(args.salida, exist_ok=True)
    for nombre in ("fotos_rtsp", "crops_rtsp"):
        enlace = os.path.join(args.salida, nombre)
        if not os.path.islink(enlace) and not os.path.exists(enlace):
            try:
                os.symlink(os.path.join("..", "..", nombre), enlace)
            except OSError:
                pass

    pares = emparejar(t1, t2)
    usados1 = {i for i, _ in pares}
    usados2 = {j for _, j in pares}
    filas = [(t1[i], t2[j]) for i, j in pares]
    filas += [(t1[i], None) for i in range(len(t1)) if i not in usados1]
    filas += [(None, t2[j]) for j in range(len(t2)) if j not in usados2]
    filas.sort(key=lambda x: (x[0] or x[1])["ts_inicio"])

    ids = _cargar_ids(args.salida)
    prox = max(ids.values(), default=0) + 1
    ids_new = {}
    for a, b in filas:
        ini = (a or b)["ts_inicio"]
        cam = (a or b)["eventos"][0]["camara"]
        clave = f"cam{cam}_{ini:.2f}"
        cid = ids.get(clave)
        if cid is None:
            cid = prox
            prox += 1
        ids_new[clave] = cid
    _guardar_ids(args.salida, ids_new)

    secciones = []
    registro = []
    for a, b in reversed(filas):
        ini = (a or b)["ts_inicio"]
        cam = (a or b)["eventos"][0]["camara"]
        cid = ids_new[f"cam{cam}_{ini:.2f}"]
        v1 = votos_de(a) if a else Counter()
        v2 = votos_de(b) if b else Counter()
        cod1, n1 = mejor_codigo(v1)
        cod2, n2 = mejor_codigo(v2)
        codf, nf = mejor_codigo(v1 + v2)
        disc = []
        if cod1 and cod2 and ocr_codes._levenshtein(cod1, cod2) > 2:
            disc.append("DISCREPANCIA")
        if cod1 and not cod2:
            disc.append("cam2 no leyó código")
        if cod2 and not cod1:
            disc.append("cam1 no leyó código")
        if not cod1 and not cod2:
            disc.append("CÓDIGO NO LEGIBLE")
        s1 = sello_de(a) if a else None
        s2 = sello_de(b) if b else None
        sf = sello_fusion(s1 or {"cls": None, "conf": None},
                          s2 or {"cls": None, "conf": None})
        clase = {"CON SELLO": "con", "SIN SELLO": "sin",
                 "DUDA": "duda"}[sf["veredicto"]]
        partes = []
        for lado, t, codc in (("cam1", a, cod1), ("cam2", b, cod2)):
            if not t:
                partes.append(f"<div><b>{lado}</b>: sin datos</div>")
                continue
            html_p = []
            fotos, foto_crops, crops = fotos_de(t)
            mejor = mejor_foto(t)
            if mejor:
                f, fc = mejor
                fig = (f"<figure><a href='{f}' target='_blank'>"
                       f"<img src='{f}'></a>"
                       f"<figcaption>{lado} {os.path.basename(f)}</figcaption>"
                       f"</figure>")
                if fc:
                    fig += (f"<figure><a href='{fc}' target='_blank'>"
                            f"<img class='crop' src='{fc}'></a>"
                            f"<figcaption>recorte sello (click = real)"
                            f"</figcaption></figure>")
                html_p.append(fig)
            if crops:
                best_crop = max(crops, key=lambda c: int(
                    c.rsplit("_a", 1)[1].split(".")[0]))
                html_p.append(
                    f"<figure><a href='{best_crop}' target='_blank'>"
                    f"<img class='crop' src='{best_crop}'></a>"
                    f"<figcaption>{lado} mejor crop código "
                    f"(click = real)</figcaption></figure>")
            partes.append(f"<div><b>{lado}</b> {''.join(html_p)}</div>")
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
                         "cam2_sello": s2["veredicto"] if s2 else None})

    with open(os.path.join(args.salida, "containers.json"), "w") as fh:
        json.dump(registro, fh, indent=1, ensure_ascii=False)
    index = f"""<!DOCTYPE html><html lang='es'><head><meta charset='utf-8'>
<title>Contenedores EN VIVO</title><style>{CSS}</style>
<meta http-equiv='refresh' content='10'></head><body>
<h1>Contenedores — EN VIVO</h1>
<p>{len(registro)} contenedores cerrados · actualizado
{time.strftime('%H:%M:%S')}</p>{''.join(secciones)}</body></html>"""
    with open(os.path.join(args.salida, "index.html"), "w") as fh:
        fh.write(index)
    print(f"[informe] {len(registro)} contenedores "
          f"({time.strftime('%H:%M:%S')})", flush=True)


def leer_lineas(path, offs):
    out = []
    try:
        with open(path) as fh:
            fh.seek(offs.get(path, 0))
            out = fh.readlines()
            offs[path] = fh.tell()
    except FileNotFoundError:
        pass
    return out


def main():
    sys.stdout.reconfigure(line_buffering=True)
    p = argparse.ArgumentParser()
    p.add_argument("--eventos-cam1", default="eventos/cam1.jsonl")
    p.add_argument("--eventos-cam2", default="eventos/cam2.jsonl")
    p.add_argument("--salida", default="informes/containers")
    args = p.parse_args()

    evs1, evs2 = [], []
    offs = {}
    ultimo = 0.0
    while True:
        try:
            for ln in leer_lineas(args.eventos_cam1, offs):
                evs1.append(json.loads(ln))
            for ln in leer_lineas(args.eventos_cam2, offs):
                evs2.append(json.loads(ln))
            ahora = time.time()
            if ahora - ultimo >= 5.0:
                construir(evs1, evs2, args)
                ultimo = ahora
            time.sleep(1.0)
        except KeyboardInterrupt:
            print("shutdown", flush=True)
            break


if __name__ == "__main__":
    main()
