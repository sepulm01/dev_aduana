#!/usr/bin/env python3
"""Conciliacion cam1+cam2 (offline) y reporte HTML por CONTENEDOR.

Por par de segmentos: fusiona las pasadas validas de cada camara (gap
<= GAP_MERGE), las empareja por timestamp (centro <= TOL_MATCH) y arma
un contenedor por camion fisico con:
- codigo de contenedor (votos de ambas camaras; discrepancias marcadas:
  una letra distinta = variantes fuzzy <=2; camara sin lectura; codigo
  no legible)
- sello en keypoint 3 (fused: acuerdo -> CON/SIN SELLO, resto -> DUDA)
- timestamp de paso
- foto 4K de cada camara (frame con mas sellos) con bboxes de sellos,
  cls3 y kpt3 dibujados, + recortes de codigo y sello a 4K.

TODO contenedor entra al informe aunque el OCR no haya leido el codigo.

Uso: .venv/bin/python conciliar.py [salida]  (default informes/containers)"""
import datetime
import glob
import json
import os
import subprocess
import sys
from collections import Counter

import cv2
import numpy as np

import ocr_codes

BASE = os.path.dirname(os.path.abspath(__file__))
VIDEOS = os.path.join(BASE, "videos")
SALIDA = os.path.join(BASE, "informes", "containers")
FOTOS = os.path.join(SALIDA, "fotos")
GAP_MERGE = 20.0
TOL_MATCH = 20.0
GAP_CODIGO = 8
W4K, H4K = 3840, 2160
PAD_X, PAD_Y = 0.15, 0.30

CSS = """
body{background:#14161a;color:#dfe3e8;font-family:system-ui,sans-serif;
 margin:24px auto;max-width:1200px;padding:0 16px}
h1,h2{color:#fff} a{color:#7cb3ff}
section{border:1px solid #2e3340;border-radius:8px;margin:18px 0;
 padding:14px;background:#1c1f26}
.badge{display:inline-block;padding:2px 10px;border-radius:4px;font-size:13px}
.con{background:#1f5130;color:#7ee2a0}.sin{background:#4d1f1f;color:#ff9c9c}
.duda{background:#4d421f;color:#ffd27d}.code{background:#0d1117;
 color:#ffd27d;font-size:22px;font-family:monospace;padding:4px 12px;
 border-radius:6px}
.warn{background:#4d2a1f;color:#ffb27d}
.cam{display:flex;gap:16px;flex-wrap:wrap;margin-top:10px}
.cam>div{flex:1;min-width:420px}
img{max-width:100%;border:1px solid #2e3340;border-radius:4px}
figure{margin:6px 0;font-size:12px;color:#9aa4b2}
.crops{display:flex;gap:10px;flex-wrap:wrap}
img.crop{width:300px;height:200px;object-fit:contain;background:#0d1117;
 cursor:zoom-in;border:1px solid #2e3340;border-radius:4px}
"""


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def ts_base(nombre):
    ts = nombre.split("_", 1)[1]
    return datetime.datetime.strptime(ts, "%Y%m%d_%H%M%S")


def t_de(ts0, frame, fps):
    return ts0 + datetime.timedelta(seconds=frame / fps)


def votos_pass(ps):
    c = Counter()
    for p in ps:
        for det in p["detecciones"]:
            o = det.get("ocr")
            if o:
                for code, _ in o["codigos"]:
                    c[code] += 1
    return c


def mejor_codigo(c):
    if not c:
        return None, 0
    con = ocr_codes.consenso(list(c.elements()), min_votos=2,
                             max_distancia=2)
    if con:
        return con, c[con]
    best, n = c.most_common(1)[0]
    return best, n


def sello_pass(ps):
    best = None
    for p in ps:
        s = p["sellos"]
        if s["veredicto"] == "sin datos":
            continue
        n = sum(mf["n_sellos"] for mf in s.get("mejores_frames", []))
        if best is None or n > best[0]:
            best = (n, s)
    if best:
        return best[1]
    return {"veredicto": "sin datos", "cls": None, "conf": None}


def mejor_frame_sellos(ps):
    best = None
    for p in ps:
        for mf in p["sellos"].get("mejores_frames", []):
            if best is None or mf["n_sellos"] > best["n_sellos"]:
                best = mf
    return best


def mejor_frame_sellos_dets(ps):
    """Fallback: frame con mas detecciones cls0/1 entre los dets crudos."""
    best = None
    for p in ps:
        cnt = {}
        for x in p["detecciones"]:
            if x["cls"] in (0, 1):
                cnt[x["frame"]] = cnt.get(x["frame"], 0) + 1
        for f, n in cnt.items():
            if best is None or n > best[0]:
                best = (n, f)
    if best:
        return {"frame": best[1], "n_sellos": best[0], "kpt3": None}
    return None


def mejor_det_codigo(ps, codigo):
    """Frame cls3 para el crop de codigo: prefiere el que leyo el codigo
    del contenedor (o su variante), luego cualquier lectura, luego el de
    mayor confianza."""
    dets3 = [x for p in ps for x in p["detecciones"] if x["cls"] == 3]
    if not dets3:
        return None
    if codigo:
        con = [x for x in dets3 if x.get("ocr") and
               any(ocr_codes._levenshtein(c, codigo) <= 2
                   for c, _ in x["ocr"]["codigos"])]
        if con:
            return max(con, key=lambda x: x["conf"])
    con_any = [x for x in dets3
               if x.get("ocr") and x["ocr"]["codigos"]]
    if con_any:
        return max(con_any, key=lambda x: x["conf"])
    return max(dets3, key=lambda x: x["conf"])


def mejor_det_cls3(ps):
    best = None
    for p in ps:
        for det in p["detecciones"]:
            if det["cls"] == 3 and (best is None or det["conf"] > best["conf"]):
                best = det
    return best


def dets_sellos_frame(ps, frame):
    out = []
    for p in ps:
        for det in p["detecciones"]:
            if det["frame"] == frame and det["cls"] in (0, 1):
                out.append(det)
    return out


def dets_cls3_frame(ps, frame):
    return [det for p in ps for det in p["detecciones"]
            if det["frame"] == frame and det["cls"] == 3]


def pasadas_camara(d):
    return [p for p in d["pasadas"] if p["valida"]]


def tiene_puertas(p):
    return sum(1 for x in p["detecciones"] if x["cls"] in (0, 1)) >= 10


def dividir_por_codigos(p):
    """Si una pasada tiene lecturas de familias de codigo distintas en
    momentos separados (dos camiones pegados en una rafaga densa), se
    parte en el limite temporal entre ellas."""
    lects = sorted(((x["frame"], c)
                    for x in p["detecciones"] if x.get("ocr")
                    for c, _ in x["ocr"]["codigos"]))
    fam = []
    ff = {}
    for f, c in lects:
        hit = None
        for fi, reps in enumerate(fam):
            if any(ocr_codes._levenshtein(c, r) <= 2 for r in reps):
                hit = fi
                break
        if hit is None:
            hit = len(fam)
            fam.append([c])
        else:
            fam[hit].append(c)
        ff.setdefault(f, hit)
    if len(fam) <= 1:
        return [p]
    frs = sorted(ff)
    cortes = []
    prev = frs[0]
    for f in frs[1:]:
        if ff[f] != ff[prev] and f - prev >= GAP_CODIGO:
            cortes.append((prev + f) // 2)
        prev = f
    if not cortes:
        return [p]
    cortes = [p["inicio_frame"]] + cortes + [p["fin_frame"] + 1]
    out = []
    for i in range(len(cortes) - 1):
        a, b = cortes[i], cortes[i + 1]
        dets = [x for x in p["detecciones"] if a <= x["frame"] < b]
        if not dets:
            continue
        sub = dict(p)
        sub["detecciones"] = dets
        sub["inicio_frame"] = a
        sub["fin_frame"] = b - 1
        tops = [mf for mf in p["sellos"].get("mejores_frames", [])
                if a <= mf["frame"] < b]
        if tops:
            sub["sellos"] = dict(p["sellos"], mejores_frames=tops)
        else:
            sub["sellos"] = {"veredicto": "sin datos", "cls": None,
                             "conf": None}
        out.append(sub)
    return out


def dividir_por_trayectoria(p, camara, Wv):
    """Si el bbox cls3 'rebobina' al lado de entrada con area enorme
    (camion nuevo pegado en la misma rafaga), se parte en el limite.
    Un cls3 por frame (mayor conf); el salto debe persistir."""
    best = {}
    for x in p["detecciones"]:
        if x["cls"] == 3:
            if x["frame"] not in best or x["conf"] > best[x["frame"]]["conf"]:
                best[x["frame"]] = x
    dets = [best[f] for f in sorted(best)]
    umbral_chico = 1500 * (Wv / 960) ** 2
    cortes = []
    for i in range(len(dets) - 1):
        a, b = dets[i], dets[i + 1]
        dx = b["cx"] - a["cx"]
        entrada = ((camara == 1 and b["cx"] < 0.2 * Wv
                    and a["cx"] > 0.8 * Wv) or
                   (camara == 2 and b["cx"] > 0.8 * Wv
                    and a["cx"] < 0.2 * Wv))
        if not (entrada and abs(dx) >= 0.25 * Wv
                and b["area"] >= 5 * a["area"] and a["area"] < umbral_chico):
            continue
        if i + 1 < len(dets) and \
                abs(dets[i + 1]["cx"] - b["cx"]) < 0.35 * Wv:
            cortes.append((a["frame"] + b["frame"]) // 2)
    if not cortes:
        return [p]
    cortes = [p["inicio_frame"]] + sorted(cortes) + [p["fin_frame"] + 1]
    out = []
    for i in range(len(cortes) - 1):
        a, b = cortes[i], cortes[i + 1]
        dets = [x for x in p["detecciones"] if a <= x["frame"] < b]
        if not dets:
            continue
        sub = dict(p)
        sub["detecciones"] = dets
        sub["inicio_frame"] = a
        sub["fin_frame"] = b - 1
        tops = [mf for mf in p["sellos"].get("mejores_frames", [])
                if a <= mf["frame"] < b]
        if tops:
            sub["sellos"] = dict(p["sellos"], mejores_frames=tops)
        else:
            sub["sellos"] = {"veredicto": "sin datos", "cls": None,
                             "conf": None}
        out.append(sub)
    return out


def es_ruido(m):
    n = sum(len(p["detecciones"]) for p in m["pasadas"])
    return n < 8 and not votos_pass(m["pasadas"])


def merges_camara(d, ts0):
    camara = d["camara"]
    Wv = d["frame_size"][0]
    out = []
    for p in d["pasadas"]:
        if not p["valida"]:
            continue
        for sub in dividir_por_codigos(p):
            for sub2 in dividir_por_trayectoria(sub, camara, Wv):
                ini = t_de(ts0, sub2["inicio_frame"], d["fps"])
                fin = t_de(ts0, sub2["fin_frame"], d["fps"])
                prev_puertas = out and any(
                    tiene_puertas(x) for x in out[-1]["pasadas"])
                if out and (ini - out[-1]["fin"]).total_seconds() <= GAP_MERGE \
                        and not prev_puertas:
                    out[-1]["fin"] = fin
                    out[-1]["fin_frame"] = sub2["fin_frame"]
                    out[-1]["pasadas"].append(sub2)
                else:
                    out.append({"ini": ini, "fin": fin,
                                "inicio_frame": sub2["inicio_frame"],
                                "fin_frame": sub2["fin_frame"],
                                "pasadas": [sub2]})
    return [m for m in out if not es_ruido(m)]


def extraer_4k(video, frames):
    frames = sorted(set(frames))
    if not frames:
        return {}
    sel = "+".join(f"eq(n,{f})" for f in frames)
    cmd = ["ffmpeg", "-v", "error", "-y",
           "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
           "-i", video, "-vf", f"select='{sel}',hwdownload,format=nv12",
           "-vsync", "0", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, timeout=900)
    n = len(proc.stdout) // (W4K * H4K * 3)
    arr = np.frombuffer(proc.stdout[:n * W4K * H4K * 3],
                        np.uint8).reshape(n, H4K, W4K, 3)
    return {f: arr[i] for i, f in enumerate(frames[:n])}


def dibujar(img, dets_s, dets_c, kpt3, sx, sy):
    for d in dets_c:
        x1, y1, x2, y2 = [int(v * s) for v, s in
                          zip(d["bbox"], (sx, sy, sx, sy))]
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 255), 6)
        cv2.putText(img, f"cod {d['conf']:.2f}", (x1, max(30, y1 - 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 0, 255), 4)
    for d in dets_s:
        x1, y1, x2, y2 = [int(v * s) for v, s in
                          zip(d["bbox"], (sx, sy, sx, sy))]
        color = (0, 255, 255) if d["cls"] == 0 else (255, 0, 255)
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 5)
        cv2.putText(img, f"{'CON' if d['cls'] == 0 else 'SIN'} {d['conf']:.2f}",
                    (x1, max(30, y1 - 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, color, 4)
    if kpt3:
        kx, ky = int(kpt3["x"] * W4K), int(kpt3["y"] * H4K)
        cv2.circle(img, (kx, ky), 40, (0, 255, 0), 8)
        cv2.putText(img, f"kpt3 {kpt3['conf']:.2f}", (kx + 50, ky),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 255, 0), 4)
    return img


def guardar_jpg(img, path):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if ok:
        with open(path, "wb") as fh:
            fh.write(buf.tobytes())
    return ok


def crop_codigo(img, det, sx, sy):
    x1, y1, x2, y2 = [v * s for v, s in zip(det["bbox"], (sx, sy, sx, sy))]
    bw, bh = x2 - x1, y2 - y1
    px, py = int(bw * PAD_X), int(bh * PAD_Y)
    cx1, cy1 = max(0, int(x1 - px)), max(0, int(y1 - py))
    cx2, cy2 = min(W4K, int(x2 + px)), min(H4K, int(y2 + py))
    if cx2 - cx1 < 60 or cy2 - cy1 < 60:
        return None
    return img[cy1:cy2, cx1:cx2]


def crop_sello(img, kpt3, dets_s, sx, sy):
    if not kpt3:
        return None
    cx, cy = kpt3["x"] * W4K, kpt3["y"] * H4K
    half = 0.07 * W4K
    for d in dets_s:
        x1, y1, x2, y2 = [v * s for v, s in zip(d["bbox"], (sx, sy, sx, sy))]
        bw, bh = x2 - x1, y2 - y1
        bx, by = (x1 + x2) / 2, (y1 + y2) / 2
        if abs(bx - cx) <= 2.5 * bw and abs(by - cy) <= 2.5 * bh:
            half = max(half, 2.2 * bw, 2.2 * bh)
    x1, y1 = max(0, int(cx - half)), max(0, int(cy - half))
    x2, y2 = min(W4K, int(cx + half)), min(H4K, int(cy + half))
    if x2 - x1 < 60 or y2 - y1 < 60:
        return None
    return img[y1:y2, x1:x2]


def sello_fusion(s1, s2):
    c1, c2 = s1.get("cls"), s2.get("cls")
    f1, f2 = s1.get("conf"), s2.get("conf")
    if c1 is not None and c2 is not None and c1 == c2 \
            and f1 and f2 and f1 >= 0.6 and f2 >= 0.6:
        return {"veredicto": "CON SELLO" if c1 == 0 else "SIN SELLO",
                "cls": c1, "conf": min(f1, f2)}
    return {"veredicto": "DUDA", "cls": None, "conf": None}


def main():
    salida = sys.argv[1] if len(sys.argv) > 1 else SALIDA
    os.makedirs(salida, exist_ok=True)
    os.makedirs(FOTOS, exist_ok=True)

    jsons = {}
    for f in sorted(glob.glob(os.path.join(BASE, "detecciones_cam*_*.json"))):
        nombre = os.path.splitext(os.path.basename(f))[0][len("detecciones_"):]
        jsons[nombre] = json.load(open(f))

    pares = {}
    for nombre in sorted(jsons):
        cam = nombre.split("_", 1)[0]
        ts = nombre.split("_", 1)[1]
        pares.setdefault(ts, {})[cam] = nombre

    contenedores = []
    for ts in sorted(pares):
        print(f"par {ts}")
        nombres = pares[ts]
        m1, m2 = None, None
        if "cam1" in nombres:
            d1 = jsons[nombres["cam1"]]
            ts0 = ts_base(nombres["cam1"])
            m1 = merges_camara(d1, ts0)
        if "cam2" in nombres:
            d2 = jsons[nombres["cam2"]]
            ts0 = ts_base(nombres["cam2"])
            m2 = merges_camara(d2, ts0)
        m1, m2 = m1 or [], m2 or []

        emp = []
        usados = set()
        for a in m1:
            mejor = None
            for i, b in enumerate(m2):
                if i in usados:
                    continue
                dist = abs(a["ini"].timestamp() +
                           (a["fin"] - a["ini"]).total_seconds() / 2 -
                           (b["ini"].timestamp() +
                            (b["fin"] - b["ini"]).total_seconds() / 2))
                if dist <= TOL_MATCH and (mejor is None or dist < mejor[0]):
                    mejor = (dist, i, b)
            if mejor:
                usados.add(mejor[1])
                emp.append({"cam1": a, "cam2s": [mejor[2]]})
            else:
                emp.append({"cam1": a, "cam2s": []})
        for i, b in enumerate(m2):
            if i in usados:
                continue
            mejor = None
            for c in emp:
                if c["cam1"] is None:
                    continue
                dist = abs(c["cam1"]["ini"].timestamp() +
                           (c["cam1"]["fin"] - c["cam1"]["ini"]).total_seconds() / 2 -
                           (b["ini"].timestamp() +
                            (b["fin"] - b["ini"]).total_seconds() / 2))
                if dist <= TOL_MATCH and (mejor is None or dist < mejor[0]):
                    mejor = (dist, c)
            if mejor:
                mejor[1]["cam2s"].append(b)
            else:
                emp.append({"cam1": None, "cam2s": [b]})

        def juntar(ms):
            if not ms:
                return None
            if len(ms) == 1:
                return ms[0]
            return {"ini": min(m["ini"] for m in ms),
                    "fin": max(m["fin"] for m in ms),
                    "pasadas": [p for m in ms for p in m["pasadas"]]}

        for c in emp:
            contenedores.append({"ts": ts, "cam1": c["cam1"],
                                 "cam2": juntar(c["cam2s"]),
                                 "n1": nombres.get("cam1"),
                                 "n2": nombres.get("cam2"),
                                 "d1": jsons.get(nombres.get("cam1", "")),
                                 "d2": jsons.get(nombres.get("cam2", ""))})

    contenedores.sort(key=lambda c: (c["ts"], (c["cam1"] or c["cam2"])["ini"]))

    secciones = []
    registro = []
    for i, c in enumerate(contenedores, 1):
        a, b = c["cam1"], c["cam2"]
        ini = (a or b)["ini"]
        hora = ini.strftime("%H:%M:%S")

        v1 = votos_pass(a["pasadas"]) if a else Counter()
        v2 = votos_pass(b["pasadas"]) if b else Counter()
        cod1, n1 = mejor_codigo(v1)
        cod2, n2 = mejor_codigo(v2)
        tot = v1 + v2
        codf, nf = mejor_codigo(tot)

        disc = []
        if cod1 and cod2 and ocr_codes._levenshtein(cod1, cod2) > 2:
            disc.append("DISCREPANCIA")
        if cod1 and not cod2:
            disc.append("cam2 no leyó código")
        if cod2 and not cod1:
            disc.append("cam1 no leyó código")
        if not cod1 and not cod2:
            disc.append("CÓDIGO NO LEGIBLE")

        s1 = sello_pass(a["pasadas"]) if a else None
        s2 = sello_pass(b["pasadas"]) if b else None
        sf = sello_fusion(s1 or {"cls": None, "conf": None},
                          s2 or {"cls": None, "conf": None})
        clase = {"CON SELLO": "con", "SIN SELLO": "sin",
                 "DUDA": "duda"}[sf["veredicto"]]

        partes_foto = []
        for lado, m, nombre, dd in (("cam1", a, c["n1"], c["d1"]),
                                    ("cam2", b, c["n2"], c["d2"])):
            if not m:
                partes_foto.append("<div><b>cam2</b>: sin datos</div>"
                                   if lado == "cam2" else
                                   "<div><b>cam1</b>: sin datos</div>")
                continue
            sx = W4K / dd["frame_size"][0]
            sy = H4K / dd["frame_size"][1]
            mf = mejor_frame_sellos(m["pasadas"])
            if mf is None:
                mf = mejor_frame_sellos_dets(m["pasadas"])
            cod_cam = cod1 if lado == "cam1" else cod2
            detc = mejor_det_codigo(m["pasadas"], cod_cam or codf)
            if mf is None and detc is None:
                partes_foto.append(f"<div><b>{lado}</b>: sin frames</div>")
                continue
            frames = set()
            if mf:
                frames.add(mf["frame"])
            if detc and (mf is None or detc["frame"] != mf["frame"]):
                frames.add(detc["frame"])
            video = os.path.join(VIDEOS, nombre + ".mkv")
            imgs = extraer_4k(video, frames)
            html_p = []
            kpt = mf.get("kpt3") if mf else None
            if mf and mf["frame"] in imgs:
                limpia = imgs[mf["frame"]]
                img = limpia.copy()
                dets_s = dets_sellos_frame(m["pasadas"], mf["frame"])
                dets_c = dets_cls3_frame(m["pasadas"], mf["frame"])
                dibujar(img, dets_s, dets_c, kpt, sx, sy)
                fpath = os.path.join(FOTOS,
                                     f"c{i:03d}_{lado}_sello.jpg")
                guardar_jpg(img, fpath)
                if kpt:
                    cs = crop_sello(limpia, kpt, dets_s, sx, sy)
                    cs_path = ""
                    if cs is not None:
                        cs_path = f"fotos/c{i:03d}_{lado}_sello_crop.jpg"
                        guardar_jpg(cs, os.path.join(salida, cs_path))
                else:
                    cs_path = ""
                v = s1["veredicto"] if lado == "cam1" else s2["veredicto"]
                cap = (f"{lado} f{mf['frame']}: n_sellos="
                       f"{mf.get('n_sellos', 0)} sello={esc(v)}"
                       f" cls={mf.get('cls')} conf={mf.get('conf')}")
                if not kpt:
                    cap += " (kpt3 no disponible)"
                html_p.append(
                    f"<figure><img src='fotos/{os.path.basename(fpath)}'>"
                    f"<figcaption>{cap}</figcaption></figure>")
                if cs_path:
                    html_p.append(
                        f"<figure><a href='{cs_path}' target='_blank'>"
                        f"<img class='crop' src='{cs_path}'></a>"
                        f"<figcaption>recorte sello (click = real)</figcaption>"
                        f"</figure>")
            elif detc and detc["frame"] in imgs:
                limpia = imgs[detc["frame"]]
                img = limpia.copy()
                dets_c = dets_cls3_frame(m["pasadas"], detc["frame"])
                dibujar(img, [], dets_c, None, sx, sy)
                fpath = os.path.join(FOTOS,
                                     f"c{i:03d}_{lado}_sello.jpg")
                guardar_jpg(img, fpath)
                html_p.append(
                    f"<figure><img src='fotos/{os.path.basename(fpath)}'>"
                    f"<figcaption>{lado} f{detc['frame']}: solo código "
                    f"(sin sellos en esta cámara)</figcaption></figure>")
            if detc and detc["frame"] in imgs:
                limpia = imgs[detc["frame"]]
                cc = crop_codigo(limpia, detc, sx, sy)
                if cc is not None:
                    cp = f"fotos/c{i:03d}_{lado}_codigo.jpg"
                    guardar_jpg(cc, os.path.join(salida, cp))
                    txt = ""
                    o = detc.get("ocr")
                    if o:
                        txt = " | ".join(f"{cc_[0]}" for cc_ in o["codigos"])
                    html_p.append(
                        f"<figure><a href='{cp}' target='_blank'>"
                        f"<img class='crop' src='{cp}'></a>"
                        f"<figcaption>{lado} código f{detc['frame']} "
                        f"conf={detc['conf']:.2f} (click = real)<br>"
                        f"<code>{esc(txt[:50])}</code></figcaption></figure>")
            partes_foto.append(f"<div><b>{lado}</b> "
                               f"{''.join(html_p) or '—'}</div>")

        vhtml = "".join(
            f"<tr><td><code>{esc(cc_)}</code></td><td>{vv}</td></tr>"
            for cc_, vv in (v1 + v2).most_common(6))
        disc_html = "".join(f"<span class='badge warn'>{esc(x)}</span> "
                            for x in disc) or "<span class='badge ok'>OK</span>"
        secciones.append(
            f"<section><h2>#{i} · {hora} · "
            f"<span class='code'>{esc(codf) if codf else 'NO LEGIBLE'}</span>"
            f" <span class='badge {clase}'>{esc(sf['veredicto'])}</span></h2>"
            f"<p>{disc_html}"
            f" cam1={esc(cod1) if cod1 else '—'}({n1}) · "
            f"cam2={esc(cod2) if cod2 else '—'}({n2}) · "
            f"sello cam1={esc(s1['veredicto']) if s1 else '—'} · "
            f"sello cam2={esc(s2['veredicto']) if s2 else '—'}</p>"
            f"<div class='cam'>{''.join(partes_foto)}</div>"
            f"<table><tr><th>código</th><th>lecturas</th></tr>{vhtml}"
            f"</table></section>")
        registro.append({"id": i, "ts": c["ts"], "hora": hora,
                         "codigo": codf, "disc": disc,
                         "sello": sf["veredicto"],
                         "cam1_codigo": cod1, "cam2_codigo": cod2,
                         "cam1_sello": s1["veredicto"] if s1 else None,
                         "cam2_sello": s2["veredicto"] if s2 else None})

    with open(os.path.join(salida, "containers.json"), "w") as fh:
        json.dump(registro, fh, indent=1, ensure_ascii=False)
    index = f"""<!DOCTYPE html><html lang='es'><head><meta charset='utf-8'>
<title>Contenedores cam1+cam2</title><style>{CSS}</style></head><body>
<h1>Contenedores — cam1 + cam2</h1>
<p>{len(contenedores)} contenedores · código con discrepancias marcadas ·
sello en keypoint 3 · foto 4K de cada cámara con recortes.</p>
{''.join(secciones)}
</body></html>"""
    with open(os.path.join(salida, "index.html"), "w") as fh:
        fh.write(index)
    print(f"informe en {salida}/index.html ({len(contenedores)} contenedores)")


if __name__ == "__main__":
    main()
