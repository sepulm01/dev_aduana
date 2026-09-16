#!/usr/bin/env python3
"""Informe web estatico: pagina HTML con los camiones en orden cronologico,
codigo + sello en cierre #3 + fotos grandes de ambas camaras, con recortes
del sello en keypoint #3 y del codigo del contenedor. Se abre localmente."""
import html
import json
import os
import sqlite3
import time

import cv2

import estado

SALIDA = os.path.join(estado.BASE, "informes", "web")
CROPS = os.path.join(SALIDA, "crops")

PAD_CODIGO_X, PAD_CODIGO_Y = 0.15, 0.30


def _crop_rect(cx, cy, half, W, H):
    x1 = max(0, int(cx - half))
    y1 = max(0, int(cy - half))
    x2 = min(W, int(cx + half))
    y2 = min(H, int(cy + half))
    if x2 - x1 < 40 or y2 - y1 < 40:
        return None
    return (x1, y1, x2, y2)


def recorte_sello(img, sello):
    """Recorte centrado en kpt3, dimensionado por el bbox del sello
    asociado mas cercano."""
    kpt = sello.get("kpt3")
    if not kpt:
        return None
    H, W = img.shape[:2]
    cx, cy = kpt["x"] * W, kpt["y"] * H
    half = 0.07 * W
    for det in sello.get("detecciones_sellos", []):
        bw, bh = (det["bbox"][2] - det["bbox"][0]) * W, \
            (det["bbox"][3] - det["bbox"][1]) * H
        bx = (det["bbox"][0] + det["bbox"][2]) / 2 * W
        by = (det["bbox"][1] + det["bbox"][3]) / 2 * H
        if abs(bx - cx) <= 2.5 * bw and abs(by - cy) <= 2.5 * bh:
            half = max(half, 2.2 * bw, 2.2 * bh)
    rect = _crop_rect(cx, cy, half, W, H)
    if not rect:
        return None
    x1, y1, x2, y2 = rect
    return img[y1:y2, x1:x2]


def recorte_codigo(img, dets_codigo):
    """Recorte del bbox de codigo de mayor confianza con margen."""
    if not dets_codigo:
        return None
    H, W = img.shape[:2]
    best = max(dets_codigo, key=lambda d: d["conf"])
    x1, y1, x2, y2 = best["bbox"]
    bw, bh = x2 - x1, y2 - y1
    px, py = int(bw * W * PAD_CODIGO_X), int(bh * H * PAD_CODIGO_Y)
    cx1 = max(0, int(x1 * W) - px)
    cy1 = max(0, int(y1 * H) - py)
    cx2 = min(W, int(x2 * W) + px)
    cy2 = min(H, int(y2 * H) + py)
    if cx2 - cx1 < 40 or cy2 - cy1 < 40:
        return None
    return img[cy1:cy2, cx1:cx2]


def _guardar(img, path):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if ok:
        with open(path, "wb") as fh:
            fh.write(buf.tobytes())
    return ok


def _fmt_hora(ts):
    return time.strftime("%d-%m %H:%M", time.localtime(ts))


def main():
    os.makedirs(CROPS, exist_ok=True)
    conn = estado.conectar()
    filas = conn.execute(
        "SELECT id, t_inicio, t_fin, registro FROM camiones "
        "ORDER BY t_inicio").fetchall()
    conn.close()

    n_total = len(filas)
    resumen = {"CON SELLO": 0, "SIN SELLO": 0, "DUDA": 0, "sin datos": 0}
    bloques = []
    for cid, t0, t1, reg_path in filas:
        try:
            with open(os.path.join(estado.BASE, reg_path)) as fh:
                reg = json.load(fh)
        except Exception:
            continue
        cod = reg.get("codigo", {}) or {}
        sell = reg.get("sello_kpt3", {}) or {}
        resumen[sell.get("veredicto", "sin datos")] = \
            resumen.get(sell.get("veredicto", "sin datos"), 0) + 1

        partes = [f'<section class="camion">']
        cabecera = []
        cabecera.append(f'<span class="hora">{_fmt_hora(t0)} — '
                        f'{_fmt_hora(t1)}</span>')
        if cod.get("codigo"):
            cabecera.append(f'<span class="codigo">{html.escape(cod["codigo"])}</span>')
            if cod.get("tier"):
                cabecera.append(f'<span class="badge tier">{html.escape(cod["tier"])}</span>')
            if cod.get("fuente"):
                cabecera.append(f'<span class="badge fuente">{html.escape(cod["fuente"])}</span>')
            if cod.get("duda"):
                cabecera.append('<span class="badge alarma">DUDA código</span>')
        else:
            cabecera.append('<span class="codigo">sin código</span>')
            cabecera.append('<span class="badge alarma">sin código</span>')
        if reg.get("parcial"):
            cabecera.append('<span class="badge aviso">parcial</span>')
        partes.append(f'<h2>{" ".join(cabecera)}</h2>')

        detalle = sell.get("detalle") or sell.get("veredicto") or "sin datos"
        clase_sello = "alarma" if sell.get("duda") else ""
        partes.append(
            f'<p class="sello {clase_sello}">Sello en cierre #3: '
            f'<b>{html.escape(sell.get("veredicto", "sin datos"))}</b> — '
            f'{html.escape(detalle)}</p>')

        partes.append('<div class="fotos">')
        for cam in ("cam1", "cam2"):
            datos = reg.get("camaras", {}).get(cam)
            partes.append('<figure>')
            if datos and datos.get("foto"):
                src = os.path.join("..", "..", datos["foto"])
                partes.append(f'<img class="foto" src="{src}" alt="{cam}">')
                cap = f'{cam} · frame {datos.get("frame", "?")} · '
                cap += f'{datos.get("n_con", 0)} con + '
                cap += f'{datos.get("n_sin", 0)} sin'
                partes.append(f'<figcaption>{cap}</figcaption>')
                img = cv2.imread(os.path.join(estado.BASE, datos["foto"]))
                if img is not None:
                    sello = datos.get("sello") or {}
                    sello = dict(sello)
                    sello["detecciones_sellos"] = \
                        (datos.get("detecciones") or {}).get("sellos", [])
                    dets_codigo = (datos.get("detecciones") or {}).get(
                        "codigo", [])
                    partes.append('<div class="recortes">')
                    rec_sello = recorte_sello(img, sello)
                    if rec_sello is not None:
                        path = os.path.join(CROPS, f"{cid}_{cam}_sello.jpg")
                        if _guardar(rec_sello, path):
                            partes.append(
                                f'<figure class="recorte"><img '
                                f'src="crops/{cid}_{cam}_sello.jpg">'
                                f'<figcaption>sello kpt3 '
                                f'({sello.get("kpt3", {}).get("conf", 0):.2f})'
                                f'</figcaption></figure>')
                    rec_cod = recorte_codigo(img, dets_codigo)
                    if rec_cod is not None:
                        path = os.path.join(CROPS, f"{cid}_{cam}_codigo.jpg")
                        if _guardar(rec_cod, path):
                            conf = max(d["conf"] for d in dets_codigo)
                            partes.append(
                                f'<figure class="recorte"><img '
                                f'src="crops/{cid}_{cam}_codigo.jpg">'
                                f'<figcaption>código ({conf:.2f})'
                                f'</figcaption></figure>')
                    partes.append('</div>')
            else:
                partes.append('<figcaption class="sinfoto">'
                              f'{cam}: no detectado</figcaption>')
            partes.append('</figure>')
        partes.append('</div></section>')
        bloques.append("".join(partes))

    resumen_html = " · ".join(f"{k}: {v}" for k, v in resumen.items())
    html_doc = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Informe de camiones — alt_batch2</title>
<style>
  body {{ font-family: sans-serif; background: #f4f4f5; color: #18181b;
         margin: 0; }}
  header {{ background: #18181b; color: #fafafa; padding: 18px 28px;
           position: sticky; top: 0; }}
  header h1 {{ margin: 0; font-size: 22px; }}
  header p {{ margin: 6px 0 0; color: #a1a1aa; font-size: 13px; }}
  main {{ max-width: 1500px; margin: 0 auto; padding: 16px; }}
  section.camion {{ background: #fff; border-radius: 8px; margin: 14px 0;
                   padding: 16px 18px; box-shadow: 0 1px 3px #0003; }}
  h2 {{ margin: 0 0 6px; font-size: 19px; }}
  .hora {{ color: #52525b; font-size: 14px; margin-right: 10px; }}
  .codigo {{ font-family: monospace; font-size: 22px; font-weight: 700; }}
  .badge {{ font-size: 12px; border-radius: 4px; padding: 2px 7px;
           margin-left: 6px; vertical-align: middle; }}
  .tier {{ background: #e4e4e7; color: #3f3f46; }}
  .fuente {{ background: #dbeafe; color: #1d4ed8; }}
  .alarma {{ background: #fee2e2; color: #b91c1c; font-weight: 700; }}
  .aviso {{ background: #fef3c7; color: #b45309; }}
  p.sello {{ margin: 4px 0 10px; font-size: 15px; }}
  p.sello.alarma {{ color: #b91c1c; }}
  .fotos {{ display: flex; gap: 14px; flex-wrap: wrap; }}
  .fotos > figure {{ margin: 0; width: 48%; min-width: 420px; }}
  img.foto {{ width: 100%; border-radius: 6px; border: 1px solid #d4d4d8; }}
  figcaption {{ font-size: 12px; color: #52525b; margin: 4px 0; }}
  figcaption.sinfoto {{ padding: 12px; background: #fafafa;
                       border: 1px dashed #d4d4d8; border-radius: 6px; }}
  .recortes {{ display: flex; gap: 10px; margin-top: 6px; }}
  .recorte img {{ max-height: 220px; border-radius: 4px;
                 border: 1px solid #d4d4d8; }}
  .recorte figcaption {{ text-align: center; }}
</style>
</head>
<body>
<header><h1>Informe de camiones — alt_batch2</h1>
<p>{n_total} camiones en orden cronológico · {resumen_html}</p></header>
<main>
{''.join(bloques)}
</main>
</body>
</html>
"""
    out = os.path.join(SALIDA, "index.html")
    with open(out, "w") as fh:
        fh.write(html_doc)
    n_crops = len(os.listdir(CROPS))
    print(f"index.html generado: {out} ({os.path.getsize(out) / 1e6:.1f} MB, "
          f"{n_total} camiones, {n_crops} recortes)")


if __name__ == "__main__":
    main()
