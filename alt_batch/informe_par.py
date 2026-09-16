#!/usr/bin/env python3
"""Informe unificado por par de camaras: una pagina por camion con la foto
4K de ambas camaras (sellos verde/rojo + codigo rojo grueso), el codigo
fusionado con la evidencia de cada camara y el veredicto del sello en el
cierre #3 (con banner de DUDA cuando aplica)."""
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

from foto_util import leer_frame_original


def _abrir_video(nombre):
    video = os.path.join("videos", f"{nombre}.mkv")
    cap = cv2.VideoCapture(video)
    base = nombre
    proxy_path = os.path.join("videos", ".proxy", base + "_h540.mp4")
    cap_proxy = cv2.VideoCapture(proxy_path) if os.path.exists(proxy_path) \
        else None
    db = os.path.join("procesados", nombre, "procesamiento.db")
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    delta = int(cur.execute("SELECT v FROM meta WHERE k='proxy_offset'")
                .fetchone()[0])
    return cap, cap_proxy, conn, cur, delta


def _foto_anotada(cur, cap, cap_proxy, delta, frame, tmp_dir):
    img = leer_frame_original(cap, frame, delta, cap_proxy)
    if img is None:
        return None, None, None
    w_img, h_img = img.shape[1], img.shape[0]
    n_con = n_sin = 0
    for cls, x1, y1, x2, y2 in cur.execute(
            "SELECT cls, x1,y1,x2,y2 FROM dets WHERE frame=? "
            "AND cls IN (0,1)", (frame,)):
        color = (0, 200, 0) if cls == 0 else (0, 0, 255)
        cv2.rectangle(img, (int(x1 * w_img), int(y1 * h_img)),
                      (int(x2 * w_img), int(y2 * h_img)), color, 5)
        n_con += cls == 0
        n_sin += cls == 1
    for x1, y1, x2, y2 in cur.execute(
            "SELECT x1,y1,x2,y2 FROM dets WHERE frame=? AND cls=3 "
            "ORDER BY conf DESC LIMIT 3", (frame,)):
        cv2.rectangle(img, (int(x1 * w_img), int(y1 * h_img)),
                      (int(x2 * w_img), int(y2 * h_img)), (0, 0, 255), 10)
    tmp = os.path.join(tmp_dir, f"ev_{os.getpid()}_{id(img)}.jpg")
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        return None, n_con, n_sin
    with open(tmp, "wb") as fh:
        fh.write(buf.tobytes())
    return tmp, n_con, n_sin


def generar_pdf(par, out=None, tmp_dir=None):
    out = out or os.path.join("procesados", "informes", f"{par}.pdf")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp_dir = tmp_dir or os.path.dirname(out)
    conn = sqlite3.connect(os.path.join("procesados", "pares.db"))
    cur = conn.cursor()
    camiones = cur.execute(
        "SELECT idx, cam1_rangos, cam2_rangos, codigo, tier, fuente, "
        "duda_codigo, cam1_codigo, cam1_tier, cam2_codigo, cam2_tier, "
        "cam1_frame, cam2_frame FROM camiones WHERE par=? ORDER BY idx",
        (par,)).fetchall()
    cierres = {}
    for row in cur.execute(
            "SELECT idx, cierre, seal3, seal3_conf, veredicto, detalle, "
            "duda, cam1_cls, cam1_conf, cam2_cls, cam2_conf, cam1_frame, "
            "cam2_frame FROM cierres WHERE par=?", (par,)):
        cierres.setdefault(row[0], []).append(row[1:])
    conn.close()

    v1 = _abrir_video(f"cam1_{par}")
    v2 = _abrir_video(f"cam2_{par}")
    try:
        _construir_pdf(par, out, tmp_dir, camiones, cierres, v1, v2)
    finally:
        for cap, cap_proxy, conn_, _, _ in (v1, v2):
            cap.release()
            cap_proxy.release()
            conn_.close()
    return out


def _construir_pdf(par, out, tmp_dir, camiones, cierres, v1, v2):
    doc = SimpleDocTemplate(
        out, pagesize=landscape(A4), leftMargin=1.2 * cm,
        rightMargin=1.2 * cm, topMargin=1.0 * cm, bottomMargin=1.0 * cm,
        title=f"Informe unificado {par}")
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1x", parent=styles["Title"], fontSize=20,
                        spaceAfter=2)
    h2 = ParagraphStyle("h2x", parent=styles["Heading2"], fontSize=14,
                        spaceBefore=10, spaceAfter=4,
                        textColor=colors.HexColor("#1f2937"))
    codigo_ok = ParagraphStyle("codeok", parent=styles["Heading1"],
                               fontSize=26,
                               textColor=colors.HexColor("#1a7f37"))
    codigo_duda = ParagraphStyle("codeduda", parent=styles["Heading1"],
                                 fontSize=26,
                                 textColor=colors.HexColor("#c81e1e"))
    seal_ok = ParagraphStyle("sealok", parent=styles["BodyText"],
                             fontSize=10)
    seal_duda = ParagraphStyle("sealduda", parent=styles["BodyText"],
                               fontSize=10,
                               textColor=colors.HexColor("#c81e1e"))
    celda = ParagraphStyle("celda", parent=styles["BodyText"], fontSize=9)

    story = []
    temporales = []
    story.append(Paragraph(f"Informe unificado — par {par}", h1))
    story.append(Paragraph(
        f"<font size=9>Contraste de lecturas cam1 + cam2 · "
        f"{len(camiones)} camiones · sellos: verde=con, rojo=sin · "
        f"código: rojo grueso · cierre #3 con veredicto fusionado</font>",
        styles["BodyText"]))

    for fila in camiones:
        (idx, r1s, r2s, codigo, tier, fuente, duda_cod,
         c1_code, c1_tier, c2_code, c2_tier, f1, f2) = fila
        titulo = f"Camión {idx}"
        if codigo:
            titulo += f" — {codigo}"
            badge = ""
            if fuente:
                badge = (f"<font size=10 color=#6b7280>fuente: {fuente}"
                         f"</font> &nbsp; ")
            if duda_cod:
                badge += ("<font size=10 color=#c81e1e><b>DUDA "
                          "(código)</b></font> &nbsp; ")
            if tier:
                badge += f"<font size=10 color=#6b7280>tier: {tier}</font>"
            st = codigo_ok if not duda_cod else codigo_duda
            story.append(Paragraph(titulo, st))
            if badge:
                story.append(Paragraph(badge, styles["BodyText"]))
        else:
            story.append(Paragraph(f"{titulo} — sin código", codigo_duda))
            story.append(Paragraph(
                "<font size=10 color=#c81e1e><b>DUDA (sin código "
                "fusionable)</b></font>", styles["BodyText"]))

        story.append(Paragraph(
            f"<font size=8 color=#6b7280>rangos: cam1 [{r1s or '—'}] · "
            f"cam2 [{r2s or '—'}]</font>", styles["BodyText"]))
        story.append(Spacer(1, 0.2 * cm))

        fotos = []
        for cam, f, cap, cap_proxy, conn_, cur_, delta in (
                (1, f1, *v1), (2, f2, *v2)):
            nombre = f"cam{cam}"
            rangos_cam = r1s if cam == 1 else r2s
            if f is None:
                if rangos_cam:
                    fotos.append(Paragraph(
                        f"<font size=9 color=#9ca3af>sin foto disponible "
                        f"en {nombre}</font>", celda))
                else:
                    fotos.append(Paragraph(
                        f"<font size=9 color=#9ca3af>no detectado en "
                        f"{nombre}</font>", celda))
            else:
                tmp, n_con, n_sin = _foto_anotada(
                    cur_, cap, cap_proxy, delta, f, tmp_dir)
                if tmp:
                    temporales.append(tmp)
                    fotos.append([
                        Paragraph(
                            f"<font size=8 color=#6b7280>{nombre} · frame "
                            f"{f} · {n_con} con + {n_sin} sin</font>",
                            styles["BodyText"]),
                        Spacer(1, 0.05 * cm),
                        Image(tmp, width=12.6 * cm,
                              height=12.6 * cm * 9 / 16),
                    ])
                else:
                    fotos.append(Paragraph(
                        f"<font size=9 color=#9ca3af>{nombre} frame {f} "
                        f"ilegible</font>", celda))
        t_fotos = Table([fotos], colWidths=[13.15 * cm, 13.15 * cm])
        t_fotos.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 1),
            ("RIGHTPADDING", (0, 0), (-1, -1), 1),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ]))
        story.append(t_fotos)
        story.append(Spacer(1, 0.2 * cm))

        filas_cod = []
        if c1_code:
            filas_cod.append(["cam1", c1_code, c1_tier or "-"])
        else:
            filas_cod.append(["cam1", "no leído", "-"])
        if c2_code:
            filas_cod.append(["cam2", c2_code, c2_tier or "-"])
        else:
            filas_cod.append(["cam2", "no leído", "-"])
        t_cod = Table(filas_cod, colWidths=[1.5 * cm, 6 * cm, 6 * cm])
        t_cod.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f3f4f6")),
            ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        story.append(t_cod)
        story.append(Spacer(1, 0.2 * cm))

        filas_sello = cierres.get(idx, [])
        if not filas_sello:
            story.append(Paragraph(
                "<font size=9 color=#9ca3af>Sello en cierre #3: sin datos "
                "de pose en ninguna cámara</font>", styles["BodyText"]))
        for (cierre, seal3, seal3_conf, veredicto, detalle, duda,
             s1_cls, s1_conf, s2_cls, s2_conf, s1_f, s2_f) in filas_sello:
            st = seal_duda if duda else seal_ok
            extra = f" — {detalle}" if detalle else ""
            story.append(Paragraph(
                f"<b>Sello en cierre #3 (cierre {cierre}): "
                f"{veredicto}</b>{extra}", st))
            ev = []
            if s1_cls is not None:
                ev.append(f"cam1 {({0: 'CON', 1: 'SIN', -1: 'sin id.'}[s1_cls])} "
                          f"({s1_conf:.2f}) f.{s1_f}")
            if s2_cls is not None:
                ev.append(f"cam2 {({0: 'CON', 1: 'SIN', -1: 'sin id.'}[s2_cls])} "
                          f"({s2_conf:.2f}) f.{s2_f}")
            if ev:
                story.append(Paragraph(
                    f"<font size=8 color=#6b7280>{' · '.join(ev)}</font>",
                    styles["BodyText"]))
        story.append(PageBreak())

    doc.build(story)
    for tmp in temporales:
        try:
            os.remove(tmp)
        except OSError:
            pass
    print(f"PDF generado: {out} ({os.path.getsize(out) / 1e6:.1f} MB)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--par", required=True)
    p.add_argument("--out", default=None)
    args = p.parse_args()
    generar_pdf(args.par, args.out)


if __name__ == "__main__":
    main()
