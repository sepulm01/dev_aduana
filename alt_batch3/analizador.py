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
from conciliar import CSS, esc, mejor_codigo, sello_fusion
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
        out.extend(_split_trayectoria(c))
    return out


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


def camiones(recs1, recs2):
    c1 = clusters_de(recs1)
    c2 = clusters_de(recs2)
    for _ in range(2):
        fr1 = [t for c in c1 for t in fronteras_de(c)]
        fr2 = [t for c in c2 for t in fronteras_de(c)]
        c1 = aplicar_cortes(c1, lambda t: corroborado(t, c2, fr2))
        c2 = aplicar_cortes(c2, lambda t: corroborado(t, c1, fr1))
    return c1, c2


def truck_de(dets):
    cam = dets[0]["cam"]
    votes = Counter()
    for d in dets:
        if d.get("_ocr"):
            for c, _ in d["_ocr"].get("codigos", []):
                votes[c] += 1
    return {"cam": cam, "dets": dets, "votes": votes,
            "ts_inicio": dets[0]["ts"], "ts_fin": dets[-1]["ts"],
            "picos": []}


def asignar_picos(trucks, recs_cam):
    picos = [r for r in recs_cam if r["tipo"] == "pico"]
    sobra = []
    for p in picos:
        candidatos = [t for t in trucks
                      if t["ts_inicio"] - 10 <= p["ts"] <= t["ts_fin"] + 30]
        if not candidatos:
            sobra.append(p)
            continue
        t = min(candidatos,
                key=lambda t: abs(p["ts"] -
                                  (t["ts_inicio"] + t["ts_fin"]) / 2))
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
                for c, _ in e["codigos"]:
                    t["votes"][c] += 1


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
    best = None
    for p in truck["picos"]:
        if p.get("foto") and os.path.exists(p["foto"]) and \
                (best is None or p["n_sellos"] > best[0]):
            best = (p["n_sellos"], p["foto"], p.get("sello_crop"))
    return best


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
    t1 = [t for t in t1 if t["dets"] or t["picos"]]
    t2 = [t for t in t2 if t["dets"] or t["picos"]]
    aplicar_reocr(t1 + t2, os.path.join(args.salida, "reocr.json"),
                  args.server)
    for t in t1 + t2:
        t["familia"], _ = mejor_codigo(t["votes"])
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

    pares = emparejar(cerradas1, cerradas2)
    usados1 = {i for i, _ in pares}
    usados2 = {j for _, j in pares}
    filas = [(cerradas1[i], cerradas2[j]) for i, j in pares]
    filas += [(cerradas1[i], None) for i in range(len(cerradas1))
              if i not in usados1]
    filas += [(None, cerradas2[j]) for j in range(len(cerradas2))
              if j not in usados2]
    filas.sort(key=lambda x: (x[0] or x[1])["ts_inicio"])

    ids = _cargar_ids(args.salida)
    prox = max(ids.values(), default=0) + 1
    ids_new = {}
    for a, b in filas:
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
    for a, b in reversed(filas):
        ini = (a or b)["ts_inicio"]
        cam = (a or b)["cam"]
        cid = ids_new[f"cam{cam}_{ini:.2f}"]
        v1 = a["votes"] if a else Counter()
        v2 = b["votes"] if b else Counter()
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
                         "cam2_sello": s2["veredicto"] if s2 else None})

    with open(os.path.join(args.salida, "containers.json"), "w") as fh:
        json.dump(registro, fh, indent=1, ensure_ascii=False)
    index = f"""<!DOCTYPE html><html lang='es'><head><meta charset='utf-8'>
<title>Contenedores EN VIVO b3</title><style>{CSS}</style>
<meta http-equiv='refresh' content='10'></head><body>
<h1>Contenedores — EN VIVO (alt_batch3)</h1>
<p>{len(registro)} contenedores cerrados · actualizado
{time.strftime('%H:%M:%S')}</p>{''.join(secciones)}</body></html>"""
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
