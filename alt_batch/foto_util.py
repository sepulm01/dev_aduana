#!/usr/bin/env python3
"""Utilidades compartidas: frame de la foto del reporte y lectura 4K
con offset. Usadas por reporte.py y pose_evento.py para garantizar que
ambos trabajan sobre el MISMO frame de cada camion."""
import cv2


def leer_frame_original(cap, f_orig, delta, cap_proxy=None):
    """Lee el frame del original 4K que mejor corresponde a la deteccion
    f_orig del proxy.

    El offset proxy->original puede derivar a lo largo del video (fuentes
    VFR/hevc), asi que si hay proxy disponible se compara el frame proxy
    f_orig contra una ventana de candidatos del original y se elige el de
    menor diferencia de pixeles. Sin proxy, seek directo con verificacion
    y fallback secuencial."""
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    objetivo = f_orig + delta

    if cap_proxy is not None:
        cap_proxy.set(cv2.CAP_PROP_POS_FRAMES, f_orig)
        ok, ref = cap_proxy.read()
        if ok and ref is not None:
            candidatos = {}
            for df in range(-2, 3):
                cand = objetivo + df
                if cand < 0 or cand >= total:
                    continue
                cap.set(cv2.CAP_PROP_POS_FRAMES, cand)
                ok, img = cap.read()
                if not ok or img is None:
                    continue
                img2 = cv2.resize(img, (ref.shape[1], ref.shape[0]),
                                  interpolation=cv2.INTER_AREA)
                candidatos[df] = (float(
                    abs(img2.astype(float) -
                        ref.astype(float)).mean()), img)
            if not candidatos:
                return None
            orden = sorted(candidatos.items(), key=lambda t: t[1][0])
            # solo se desvia del objetivo si el mejor tiene margen claro
            if len(orden) >= 2 and orden[1][1][0] - orden[0][1][0] > 0.25:
                return orden[0][1][1]
            if 0 in candidatos:
                return candidatos[0][1]
            return orden[0][1][1]

    if objetivo < 0 or objetivo >= total:
        return None
    cap.set(cv2.CAP_PROP_POS_FRAMES, objetivo)
    ok, img = cap.read()
    if ok and img is not None:
        pos = cap.get(cv2.CAP_PROP_POS_FRAMES)
        if abs(pos - (objetivo + 1)) <= 1:
            return img
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    img = None
    for _ in range(objetivo + 1):
        ok, img = cap.read()
        if not ok:
            return None
        pos = cap.get(cv2.CAP_PROP_POS_FRAMES)
        if abs(pos - (objetivo + 1)) <= 1:
            return img
    return img


def best_frame(cur, camion, frames_csv):
    frames = [int(x) for x in frames_csv.split(",") if x]
    if not frames:
        return None
    confs = {f: c for f, c in cur.execute(
        "SELECT frame, conf FROM ocr WHERE camion=?", (camion,))}
    return max(frames, key=lambda f: confs.get(f, 0.0))


def foto_frame(cur, camion, r0, r1, frames_csvs):
    """Frame de la foto del reporte de un camion: el que tiene mas sellos
    (cls0/1) dentro del rango; fallback al mejor frame del primer codigo."""
    sel = cur.execute(
        "SELECT frame FROM dets WHERE cls IN (0,1) AND frame BETWEEN ? AND ? "
        "GROUP BY frame ORDER BY COUNT(*) DESC, frame LIMIT 1",
        (r0, r1)).fetchone()
    if sel:
        return sel[0]
    for csv_ in frames_csvs or []:
        f = best_frame(cur, camion, csv_)
        if f is not None:
            return f
    return None
