#!/usr/bin/env python3
"""Reporte PDF por video: todos los camiones en orden secuencial de aparicion
(confirmados y no-confirmados), con codigo, metadata y foto 4K con el bbox
del codigo dibujado. Un PDF por video."""
import argparse
import os
import sqlite3

import cv2
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

from foto_util import best_frame, foto_frame, leer_frame_original


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run", required=True)
    p.add_argument("--video", required=True)
    p.add_argument("--out", default=None)
    args = p.parse_args()

    db = os.path.join(args.run, "procesamiento.db")
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    video = os.path.basename(args.video)
    out = args.out or os.path.join(args.run, f"reporte_{video}.pdf")

    rangos = {r[0]: (r[1], r[2]) for r in cur.execute(
        "SELECT idx, inicio, fin FROM rangos")}
    codigos = {}
    for r in cur.execute("SELECT camion, orden, codigo, tier, peso, frames, "
                         "parciales, size_type FROM codigos ORDER BY orden"):
        codigos.setdefault(r[0], []).append(r)
    run = cur.execute(
        "SELECT elapsed_s, guardados FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    row = cur.execute(
        "SELECT v FROM meta WHERE k='proxy_offset'").fetchone()
    delta = int(row[0]) if row else 0
    tiene_pose = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='pose_cierres'").fetchone() is not None

    camiones_orden = sorted(codigos, key=lambda c: rangos[c][0])
    n_codigos = sum(len(v) for v in codigos.values())

    # frames 4K necesarios: la foto de cada camion (frame con mas sellos,
    # fallback al mejor frame del primer codigo)
    foto_plan = {}
    for c in camiones_orden:
        r0, r1 = rangos[c]
        foto_plan[c] = foto_frame(cur, c, r0, r1, [codigos[c][0][5]])

    cap = cv2.VideoCapture(args.video)
    base = os.path.splitext(os.path.basename(args.video))[0]
    proxy_path = os.path.join(os.path.dirname(os.path.abspath(args.video)),
                              ".proxy", base + "_h540.mp4")
    cap_proxy = cv2.VideoCapture(proxy_path) if os.path.exists(proxy_path) \
        else None
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    fotos = {}
    sellos_info = {}
    for c in camiones_orden:
        r0, r1 = rangos[c]
        f_foto = foto_plan[c]
        if f_foto is None:
            continue
        img = leer_frame_original(cap, f_foto, delta, cap_proxy)
        if img is None:
            continue
        f = f_foto
        w_img, h_img = img.shape[1], img.shape[0]
        n_con = n_sin = 0
        for cls, _, x1, y1, x2, y2 in cur.execute(
                "SELECT cls, conf, x1,y1,x2,y2 FROM dets WHERE frame=? "
                "AND cls IN (0,1)", (f,)):
            color = (0, 200, 0) if cls == 0 else (0, 0, 255)
            cv2.rectangle(img, (int(x1 * w_img), int(y1 * h_img)),
                          (int(x2 * w_img), int(y2 * h_img)), color, 5)
            n_con += cls == 0
            n_sin += cls == 1
        for x1, y1, x2, y2 in cur.execute(
                "SELECT x1,y1,x2,y2 FROM dets WHERE frame=? AND cls=3 "
                "ORDER BY conf DESC LIMIT 3", (f,)):
            cv2.rectangle(img, (int(x1 * w_img), int(y1 * h_img)),
                          (int(x2 * w_img), int(y2 * h_img)),
                          (0, 0, 255), 10)
        fotos[c] = img
        sellos_info[c] = (f, n_con, n_sin)
    cap.release()
    if cap_proxy is not None:
        cap_proxy.release()

    # ---------------- PDF ----------------
    doc = SimpleDocTemplate(out, pagesize=landscape(A4),
                            leftMargin=1.2 * cm, rightMargin=1.2 * cm,
                            topMargin=1.0 * cm, bottomMargin=1.0 * cm,
                            title=f"Reporte OCR {video}")
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1x", parent=styles["Title"], fontSize=20,
                        spaceAfter=2)
    h2 = ParagraphStyle("h2x", parent=styles["Heading2"], fontSize=14,
                        spaceBefore=10, spaceAfter=4,
                        textColor=colors.HexColor("#1f2937"))
    codigo_ok = ParagraphStyle("codeok", parent=styles["Heading1"],
                               fontSize=26, textColor=colors.HexColor("#1a7f37"))
    codigo_nc = ParagraphStyle("codenc", parent=styles["Heading1"],
                               fontSize=26, textColor=colors.HexColor("#b35900"))
    celda = ParagraphStyle("celda", parent=styles["BodyText"], fontSize=9)
    mono = ParagraphStyle("mono", parent=styles["BodyText"], fontSize=9,
                          fontName="Courier")

    story = []
    temporales = []
    story.append(Paragraph(f"Reporte OCR — {video}", h1))
    meta = (f"<font size=9>Corrida: {run[0]:.1f}s &nbsp;|&nbsp; "
            f"camiones con codigo: {len(camiones_orden)} &nbsp;|&nbsp; "
            f"codigos leidos: {n_codigos} &nbsp;|&nbsp; frames video: {total}"
            f" &nbsp;|&nbsp; foto 4K por camion: sellos "
            f"(verde=con, rojo=sin) + codigo (rojo grueso)</font>")
    story.append(Paragraph(meta, styles["BodyText"]))

    for i, c in enumerate(camiones_orden, 1):
        r0, r1 = rangos[c]
        story.append(Paragraph(
            f"Camión {i} de {len(camiones_orden)} · pasada frames {r0}-{r1} "
            f"({(r1 - r0) / 20:.1f}s @20fps)", h2))
        foto = fotos.get(c)
        if foto is not None:
            f_foto, n_con, n_sin = sellos_info[c]
            caption = (f"Frame {f_foto} · sellos: {n_con} con_sello + "
                       f"{n_sin} sin_sello")
            tmp = os.path.join(args.run, f"_tmp_ev_{c}_{f_foto}.jpg")
            ok, buf = cv2.imencode(".jpg", foto,
                                   [cv2.IMWRITE_JPEG_QUALITY, 90])
            if ok:
                with open(tmp, "wb") as fh:
                    fh.write(buf.tobytes())
            temporales.append(tmp)
            story.append(KeepTogether([
                Paragraph(f"<font size=8 color=#6b7280>{caption}</font>",
                          styles["BodyText"]),
                Spacer(1, 0.1 * cm),
                Image(tmp, width=19 * cm, height=19 * cm * 9 / 16),
                Spacer(1, 0.15 * cm),
            ]))
        if tiene_pose:
            pose_rows = cur.execute(
                "SELECT sello_cls, sello_conf FROM pose_cierres "
                "WHERE camion=? ORDER BY cierre", (c,)).fetchall()
            if not pose_rows:
                pose_rows = [(-1, 0.0)]
            for sello_cls, sello_conf in pose_rows:
                if sello_cls == 0:
                    texto = f"CON SELLO (conf {sello_conf:.2f})"
                elif sello_cls == 1:
                    texto = f"SIN SELLO (conf {sello_conf:.2f})"
                else:
                    texto = "sin identificar"
                story.append(Paragraph(
                    f"<font size=9 color=#374151>"
                    f"Sello en cierre #3: {texto}</font>",
                    styles["BodyText"]))
                story.append(Spacer(1, 0.15 * cm))
        for fila in codigos[c]:
            camion, orden, code, tier, peso, frames, parciales, size_type = fila
            confirmado = "noconfirmado" not in tier
            st = codigo_ok if confirmado else codigo_nc
            story.append(Paragraph(
                f"<b>{code}</b> &nbsp; "
                f"<font size=11 color=#6b7280>"
                f"{'CONFIRMADO' if confirmado else 'NO CONFIRMADO'}</font>", st))
            filas_tabla = [
                ["Tier", tier.replace(",", ", "), "Peso", str(peso)],
                ["Frames", frames, "Size/type", size_type or "-"],
                ["Parciales", parciales or "-", "", ""],
            ]
            t = Table(filas_tabla, colWidths=[2.2 * cm, 7.2 * cm, 2.2 * cm,
                                              9.5 * cm])
            t.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f3f4f6")),
                ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f3f4f6")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONT", (0, 0), (-1, -1), "Helvetica", 8.5),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            story.append(t)
            story.append(Spacer(1, 0.25 * cm))
            f = best_frame(cur, c, frames)
            crop_path = os.path.join(args.run, "crops", f"c{c}_f{f}.png")
            if os.path.exists(crop_path):
                img_crop = Image(crop_path)
                ar = img_crop.imageWidth / max(img_crop.imageHeight, 1)
                w_crop = min(6 * cm, 5 * cm * ar)
                h_crop = min(5 * cm, 6 * cm / max(ar, 1e-6))
                story.append(KeepTogether([
                    Paragraph(f"<font size=8 color=#6b7280>"
                              f"Crop OCR del frame {f}</font>",
                              styles["BodyText"]),
                    Spacer(1, 0.1 * cm),
                    Image(crop_path, width=w_crop, height=h_crop),
                ]))
            story.append(Spacer(1, 0.5 * cm))
        story.append(PageBreak())

    doc.build(story)
    for tmp in temporales:
        try:
            os.remove(tmp)
        except OSError:
            pass
    print(f"PDF generado: {out} ({os.path.getsize(out) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
