#!/usr/bin/env python3
"""Informe web estatico desde los detecciones_*.json (pipeline online).

Por par (cam1+cam2): codigos confirmados + veredicto de sello por pasada.
Por video: resumen, votos por codigo, pasadas, frames top de sellos con
kpt3 dibujado (extraidos con ffmpeg GPU) y galeria de crops OCR.

Uso: .venv/bin/python informe_json.py [salida]  (default informes/web_json)"""
import glob
import html
import os
import subprocess
import sys
from collections import Counter

import cv2
import numpy as np

import ocr_codes

BASE = os.path.dirname(os.path.abspath(__file__))
VIDEOS = os.path.join(BASE, "videos")
SALIDA = os.path.join(BASE, "informes", "web_json")
CROPS_DIR = os.path.join(BASE, "crops_area")
EVID = os.path.join(SALIDA, "evidencia")
CROPS_OUT = os.path.join(SALIDA, "crops")

CSS = """
body{background:#14161a;color:#dfe3e8;font-family:system-ui,sans-serif;
 margin:24px auto;max-width:1100px;padding:0 16px}
h1,h2{color:#fff} a{color:#7cb3ff}
table{border-collapse:collapse;width:100%;margin:10px 0;background:#1c1f26}
th,td{border:1px solid #2e3340;padding:6px 9px;text-align:left;
 vertical-align:top;font-size:13px}
th{background:#232733}
code{background:#0d1117;padding:1px 5px;border-radius:4px;color:#ffd27d}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:12px}
.con{background:#1f5130;color:#7ee2a0}.sin{background:#4d1f1f;color:#ff9c9c}
.duda{background:#4d421f;color:#ffd27d}.ok{background:#1f3d51;color:#7cb3ff}
img{max-width:100%;border:1px solid #2e3340;border-radius:4px}
.gal{display:flex;flex-wrap:wrap;gap:8px}
.gal figure{margin:4px;width:280px;font-size:11px;color:#9aa4b2}
"""


def esc(s):
    return html.escape(str(s))


def extraer_frames(video, frames, W, H):
    """Extrae frames del video (decode GPU) como lista de arrays BGR."""
    frames = sorted(set(frames))
    if not frames:
        return {}
    sel = "+".join(f"eq(n,{f})" for f in frames)
    cmd = ["ffmpeg", "-v", "error", "-y",
           "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
           "-i", video, "-vf",
           f"select='{sel}',scale_cuda={W}:{H},hwdownload,format=nv12",
           "-vsync", "0", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, timeout=600)
    n = len(proc.stdout) // (W * H * 3)
    arr = np.frombuffer(proc.stdout[:n * W * H * 3],
                        np.uint8).reshape(n, H, W, 3)
    return {f: arr[i] for i, f in enumerate(frames[:n])}


def dibujar_evidencia(img, dets_sellos, kpt3, W, H):
    for d in dets_sellos:
        x1, y1, x2, y2 = d["bbox"]
        color = (0, 255, 255) if d["cls"] == 0 else (255, 0, 255)
        cv2.rectangle(img, (int(x1), int(y1)), (int(x2), int(y2)), color, 2)
        cv2.putText(img, f"{'CON' if d['cls']==0 else 'SIN'} {d['conf']:.2f}",
                    (int(x1), max(18, int(y1) - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
    if kpt3:
        kx, ky = int(kpt3["x"] * W), int(kpt3["y"] * H)
        cv2.circle(img, (kx, ky), 14, (0, 255, 0), 3)
        cv2.putText(img, f"kpt3 {kpt3['conf']:.2f}", (kx + 18, ky),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    return img


def votos_de(d):
    votos = Counter()
    for p in d["pasadas"]:
        for det in p["detecciones"]:
            o = det.get("ocr")
            if o:
                for c, _ in o["codigos"]:
                    votos[c] += 1
    return votos


def pagina_video(base, d):
    cam = d["camara"]
    W, H = d["frame_size"]
    res = d["resumen"]
    tops = [(p, mf) for p in d["pasadas"]
            for mf in p["sellos"].get("mejores_frames", [])]
    frames_evid = {}
    if tops:
        frames_evid = extraer_frames(
            os.path.join(VIDEOS, base + ".mkv"),
            [mf["frame"] for _, mf in tops], W, H)
    os.makedirs(EVID, exist_ok=True)
    evid_html = []
    for p, mf in tops:
        img = frames_evid.get(mf["frame"])
        if img is None:
            continue
        img = img.copy()
        dets_s = [x for x in p["detecciones"]
                  if x["frame"] == mf["frame"] and x["cls"] in (0, 1)]
        dibujar_evidencia(img, dets_s, mf.get("kpt3"), W, H)
        path = os.path.join(EVID, f"{base}_f{mf['frame']}.jpg")
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if ok:
            with open(path, "wb") as fh:
                fh.write(buf.tobytes())
            sello = p["sellos"]
            evid_html.append(
                f"<figure><img src='evidencia/{os.path.basename(path)}'>"
                f"<figcaption>pasada {p['id']} frame {mf['frame']} "
                f"n_sellos={mf['n_sellos']} veredicto="
                f"<b>{esc(sello['veredicto'])}</b> cls={mf['cls']} "
                f"conf={mf['conf']}</figcaption></figure>")

    votos = votos_de(d)
    votos_html = "".join(
        f"<tr><td><code>{esc(c)}</code></td><td>{v}</td></tr>"
        for c, v in votos.most_common())

    pasadas_html = []
    for p in d["pasadas"]:
        s = p["sellos"]
        clase = {"CON SELLO": "con", "SIN SELLO": "sin",
                 "DUDA": "duda"}.get(s["veredicto"], "duda")
        extra = ""
        if s["veredicto"] != "sin datos":
            extra = (f"cls={s['cls']} conf={s['conf']} top=" +
                     ", ".join(f"f{mf['frame']}(n={mf['n_sellos']})"
                               for mf in s["mejores_frames"]))
        pasadas_html.append(
            f"<tr><td>{p['id']}</td><td>{p['inicio_frame']}-{p['fin_frame']}"
            f"</td><td>{esc(p['direccion'])}</td>"
            f"<td>{'sí' if p['valida'] else 'no'}</td>"
            f"<td><span class='badge {clase}'>{esc(s['veredicto'])}</span>"
            f" {esc(extra)}</td></tr>")

    os.makedirs(CROPS_OUT, exist_ok=True)
    gal_html = []
    for f in sorted(glob.glob(os.path.join(CROPS_DIR, base, "*.png"))):
        nombre = os.path.basename(f)
        destino = os.path.join(CROPS_OUT, f"{base}_{nombre}")
        if not os.path.exists(destino):
            try:
                with open(f, "rb") as src, open(destino, "wb") as dst:
                    dst.write(src.read())
            except OSError:
                continue
        frame = nombre[1:7]
        det = next((x for p in d["pasadas"] for x in p["detecciones"]
                    if x["frame"] == int(frame) and x["cls"] == 3
                    and x.get("enviado_ocr")), None)
        ocr_txt = ""
        if det and det.get("ocr"):
            ocr_txt = " | ".join(f"{c}({t})" for c, t in det["ocr"]["codigos"])
        gal_html.append(
            f"<figure><img src='crops/{os.path.basename(destino)}'>"
            f"<figcaption>f{frame} area={det['area'] if det else '?'} "
            f"<br><code>{esc(ocr_txt[:60])}</code></figcaption></figure>")

    conf = ", ".join(res["confirmados"]) or "ninguno"
    return f"""<!DOCTYPE html><html lang='es'><head><meta charset='utf-8'>
<title>{esc(base)}</title><style>{CSS}</style></head><body>
<p><a href='index.html'>← índice</a></p>
<h1>{esc(base)} <span class='badge ok'>cam{cam}</span></h1>
<p>{esc(d['video'])} · {res['frames']} frames · {res['inferencias']} inf ·
{res['cls3']} cls3 · {res['sellos']} sellos · {res['a_ocr']} a OCR ·
{res['lecturas']} lecturas · {res['elapsed']}s · pose {res['pose_calls']}
({res['pose_ms']}ms)</p>
<h2>Códigos confirmados</h2>
<p><span class='badge con'>{esc(conf)}</span>
&nbsp;consenso: <code>{esc(res['consenso'] or '—')}</code></p>
<table><tr><th>código</th><th>lecturas</th></tr>{votos_html}</table>
<h2>Pasadas</h2>
<table><tr><th>id</th><th>frames</th><th>dirección</th><th>válida</th>
<th>sello</th></tr>{''.join(pasadas_html)}</table>
<h2>Sello: frames con más detecciones (kpt3)</h2>
<div class='gal'>{''.join(evid_html) or '<p>sin datos</p>'}</div>
<h2>Crops enviados a OCR</h2>
<div class='gal'>{''.join(gal_html) or '<p>sin crops</p>'}</div>
</body></html>"""


def main():
    salida = sys.argv[1] if len(sys.argv) > 1 else SALIDA
    os.makedirs(salida, exist_ok=True)
    os.makedirs(EVID, exist_ok=True)
    os.makedirs(CROPS_OUT, exist_ok=True)

    jsons = {}
    for f in sorted(glob.glob(os.path.join(BASE, "detecciones_cam*_*.json"))):
        base = os.path.splitext(os.path.basename(f))[0][len("detecciones_"):]
        jsons[base] = f

    pares = {}
    for base in sorted(jsons):
        cam = base.split("_", 1)[0]
        ts = base.split("_", 1)[1]
        pares.setdefault(ts, {})[cam] = base

    filas = []
    for ts in sorted(pares):
        celdas = []
        for cam in ("cam1", "cam2"):
            base = pares[ts].get(cam)
            if not base:
                celdas.append("<td>—</td>")
                continue
            d = __import__("json").load(open(jsons[base]))
            cods = _unicos(d["resumen"]["confirmados"])
            sellos = [p["sellos"]["veredicto"].replace(" SELLO", "")
                      for p in d["pasadas"] if p["valida"]
                      and p["sellos"]["veredicto"] != "sin datos"]
            shtml = " ".join(
                f"<span class='badge "
                f"{'con' if s=='CON' else 'sin' if s=='SIN' else 'duda'}'>"
                f"{esc(s)}</span>" for s in dict.fromkeys(sellos))
            pagina = f"{base}.html"
            with open(os.path.join(salida, pagina), "w") as fh:
                fh.write(pagina_video(base, d))
            celdas.append(
                f"<td><a href='{pagina}'>{esc(base)}</a><br>"
                f"<code>{esc(', '.join(cods) or '—')}</code><br>{shtml}</td>")
        filas.append(f"<tr><td><b>{esc(ts)}</b></td>{''.join(celdas)}</tr>")

    index = f"""<!DOCTYPE html><html lang='es'><head><meta charset='utf-8'>
<title>Informe cam1+cam2 (pipeline online)</title><style>{CSS}</style>
</head><body><h1>Informe cam1+cam2 — pipeline online</h1>
<p>{len(pares)} pares · código confirmado y veredicto de sello por cámara.
Haz clic en un video para el detalle (pasadas, kpt3, crops OCR).</p>
<table><tr><th>par</th><th>cam1</th><th>cam2</th></tr>{''.join(filas)}
</table></body></html>"""
    with open(os.path.join(salida, "index.html"), "w") as fh:
        fh.write(index)
    print(f"informe en {salida}/index.html ({len(pares)} pares)")


def _unicos(codigos):
    out = []
    for c in codigos:
        if any(ocr_codes._levenshtein(c, o) <= 2 for o in out):
            continue
        out.append(c)
    return out


if __name__ == "__main__":
    main()
