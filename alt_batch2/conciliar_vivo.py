#!/usr/bin/env python3
"""Conciliador EN VIVO: consume los JSONL de eventos de ambas camaras y
regenera el informe por contenedor a medida que llegan datos.

Uso: .venv/bin/python conciliar_vivo.py --eventos-cam1 eventos/cam1.jsonl \
       --eventos-cam2 eventos/cam2.jsonl [--salida informes/containers]
"""
import argparse
import json
import os
import sys
import time
from collections import Counter

from conciliar import (CSS, GAP_MERGE, TOL_MATCH, esc, mejor_codigo,
                       sello_fusion, tiene_puertas, es_ruido,
                       dividir_por_codigos, dividir_por_trayectoria)


def evento_a_pasada(ev):
    dets = []
    for f, cx, cy, area, conf, cod in ev["cls3"]:
        det = {"frame": f, "cls": 3, "conf": conf, "cx": cx, "cy": cy,
               "area": area, "enviado_ocr": False}
        if cod:
            det["ocr"] = {"codigos": [[cod, "raw"]], "texto": ""}
        dets.append(det)
    s = ev["sellos"]
    return {"id": ev["bid"], "inicio_frame": ev["frame_inicio"],
            "fin_frame": ev["frame_fin"], "valida": ev["valida"],
            "detecciones": dets, "_n_sellos": ev["n_sellos"],
            "_n_dets": ev["n_cls3"] + ev["n_sellos"],
            "_evento": ev,
            "sellos": {"veredicto": s["veredicto"], "cls": s.get("cls"),
                       "conf": s.get("conf"),
                       "duda": s.get("duda", True),
                       "mejores_frames": [
                           {"frame": t["frame"], "n_sellos": t["n_sellos"],
                            "cls": t.get("cls"), "conf": t.get("conf")}
                           for t in ev["sellos_tops"]]}}


def pasadas_camara(evs, camara, Wv, fps=20.0):
    out = []
    for ev in evs:
        p = evento_a_pasada(ev)
        if not p["valida"]:
            continue
        dur = max(ev["ts_fin"] - ev["ts_inicio"], 0.1)
        span = max(ev["frame_fin"] - ev["frame_inicio"], 1)
        for sub in dividir_por_codigos(p):
            for sub2 in dividir_por_trayectoria(sub, camara, Wv):
                f0 = sub2["inicio_frame"] - ev["frame_inicio"]
                f1 = sub2["fin_frame"] - ev["frame_inicio"]
                ini = ev["ts_inicio"] + dur * f0 / span
                fin = ev["ts_inicio"] + dur * f1 / span
                prev_puertas = out and any(
                    tiene_puertas(x) for x in out[-1]["pasadas"])
                if out and ini - out[-1]["fin"] <= GAP_MERGE \
                        and not prev_puertas:
                    out[-1]["fin"] = fin
                    out[-1]["pasadas"].append(sub2)
                else:
                    out.append({"ini": ini, "fin": fin,
                                "pasadas": [sub2], "_evento": ev})
    return [m for m in out if not es_ruido(m)]


def votos_pass_rtsp(ps):
    c = Counter()
    for p in ps:
        c.update(p["_evento"]["codigos_votos"])
    return c


def juntar(ms):
    if not ms:
        return None
    if len(ms) == 1:
        return ms[0]
    return {"ini": min(m["ini"] for m in ms),
            "fin": max(m["fin"] for m in ms),
            "pasadas": [p for m in ms for p in m["pasadas"]]}


def emparejar(m1, m2):
    emp = []
    usados = set()
    for a in m1:
        mejor = None
        for i, b in enumerate(m2):
            if i in usados:
                continue
            dist = abs((a["ini"] + a["fin"]) / 2 - (b["ini"] + b["fin"]) / 2)
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
            dist = abs((c["cam1"]["ini"] + c["cam1"]["fin"]) / 2 -
                       (b["ini"] + b["fin"]) / 2)
            if dist <= TOL_MATCH and (mejor is None or dist < mejor[0]):
                mejor = (dist, c)
        if mejor:
            mejor[1]["cam2s"].append(b)
        else:
            emp.append({"cam1": None, "cam2s": [b]})
    return [(c["cam1"], juntar(c["cam2s"])) for c in emp]


def sello_pass(ps):
    best = None
    for p in ps:
        s = p["sellos"]
        if s["veredicto"] == "sin datos":
            continue
        n = sum(mf["n_sellos"] for mf in s.get("mejores_frames", []))
        if best is None or n > best[0]:
            best = (n, s)
    return best[1] if best else {"veredicto": "sin datos", "cls": None,
                                 "conf": None}


def hora(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts))


def construir(evs1, evs2, camara, salida):
    Wv = 3840
    evs1 = sorted(evs1, key=lambda e: e["ts_inicio"])
    evs2 = sorted(evs2, key=lambda e: e["ts_inicio"])
    m1 = pasadas_camara(evs1, 1, Wv)
    m2 = pasadas_camara(evs2, 2, Wv)
    os.makedirs(salida, exist_ok=True)
    for nombre in ("fotos_rtsp", "crops_rtsp"):
        enlace = os.path.join(salida, nombre)
        if not os.path.islink(enlace) and not os.path.exists(enlace):
            try:
                os.symlink(os.path.join("..", "..", nombre), enlace)
            except OSError:
                pass
    pares = sorted(emparejar(m1, m2),
                   key=lambda x: (x[0] or x[1])["ini"], reverse=True)
    secciones = []
    registro = []
    for i, (a, b) in enumerate(pares, 1):
        ini = (a or b)["ini"]
        v1 = votos_pass_rtsp(a["pasadas"]) if a else Counter()
        v2 = votos_pass_rtsp(b["pasadas"]) if b else Counter()
        cod1, n1 = mejor_codigo(v1)
        cod2, n2 = mejor_codigo(v2)
        codf, nf = mejor_codigo(v1 + v2)
        disc = []
        if cod1 and cod2 and cod1 != cod2:
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
        partes = []
        for lado, m, codc in (("cam1", a, cod1), ("cam2", b, cod2)):
            if not m:
                partes.append(f"<div><b>{lado}</b>: sin datos</div>")
                continue
            html_p = []
            fotos = []
            for p in m["pasadas"]:
                ev = p["_evento"]
                for i, f in enumerate(ev["fotos"]):
                    if f not in fotos:
                        fotos.append((f, ev.get("foto_crops", [None] * len(
                            ev["fotos"]))[i] if i < len(ev["fotos"]) else None))
            for f, fc in fotos[:4]:
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
            crops = [c for p in m["pasadas"] for c in p["_evento"]["crops"]]
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
            f"<section><h2>#{i} · {hora(ini)} · "
            f"<span class='code'>{esc(codf) if codf else 'NO LEGIBLE'}</span>"
            f" <span class='badge {clase}'>{esc(sf['veredicto'])}</span></h2>"
            f"<p>{disc_html} cam1={esc(cod1) if cod1 else '—'}({n1}) · "
            f"cam2={esc(cod2) if cod2 else '—'}({n2}) · "
            f"sello cam1={esc(s1['veredicto']) if s1 else '—'} · "
            f"sello cam2={esc(s2['veredicto']) if s2 else '—'}</p>"
            f"<div class='cam'>{''.join(partes)}</div></section>")
        registro.append({"id": i, "ts_inicio": round(ini),
                         "hora": hora(ini), "codigo": codf, "disc": disc,
                         "sello": sf["veredicto"],
                         "cam1_codigo": cod1, "cam2_codigo": cod2,
                         "cam1_sello": s1["veredicto"] if s1 else None,
                         "cam2_sello": s2["veredicto"] if s2 else None})
    os.makedirs(salida, exist_ok=True)
    with open(os.path.join(salida, "containers.json"), "w") as fh:
        json.dump(registro, fh, indent=1, ensure_ascii=False)
    index = f"""<!DOCTYPE html><html lang='es'><head><meta charset='utf-8'>
<title>Contenedores EN VIVO</title><style>{CSS}</style>
<meta http-equiv='refresh' content='10'></head><body>
<h1>Contenedores — EN VIVO</h1>
<p>{len(registro)} contenedores · actualizado {time.strftime('%H:%M:%S')}
</p>{''.join(secciones)}</body></html>"""
    with open(os.path.join(salida, "index.html"), "w") as fh:
        fh.write(index)
    print(f"[informe] {len(registro)} contenedores "
          f"({time.strftime('%H:%M:%S')})", flush=True)


def leer_lineas(path, offs):
    """Devuelve lineas nuevas desde el offset."""
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
                construir(evs1, evs2, None, args.salida)
                ultimo = ahora
            time.sleep(1.0)
        except KeyboardInterrupt:
            print("shutdown", flush=True)
            break


if __name__ == "__main__":
    main()
