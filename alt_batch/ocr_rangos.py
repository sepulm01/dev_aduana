#!/usr/bin/env python3
"""Etapa OCR del pipeline offline.

Por camion: cosecha hasta N crops cls3 repartidos en el tiempo de la pasada
(esquina -> puerta), OCR secuencial con EARLY EXIT cuando 2 crops coinciden
en el mismo codigo (consenso min_votos=2). Si un crop da un size/type code
(42G1/45R1...) sin codigo ISO, reintenta con padding ampliado (el codigo
esta cerca de la marca de dimensiones). Texto vertical: rotacion 90 grados.

DB: tabla 'ocr' (por crop), 'ocr_codigos' (codigo por frame), 'codigos'
(codigo final por camion + votos + parciales con '#' + size/type).
"""
import argparse
import os
import sqlite3
import time

import cv2
import requests

import ocr_codes

PAD_X, PAD_Y = 0.15, 0.30
PAD_RETRY_X, PAD_RETRY_Y = 0.75, 0.75


def ocr_buf(buf, server):
    r = requests.post(server + "/ocr", files={"file": buf}, timeout=180)
    r.raise_for_status()
    return r.json().get("text", "")


def ocr_tall(img, server):
    """Crop vertical: rotar 90 grados; si sigue muy largo, bandas solapadas."""
    textos = []
    rot = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if rot.shape[0] > 900:
        band_h, step = 700, 500
        for x0 in range(0, rot.shape[0] - band_h + 1, step):
            _, buf = cv2.imencode(".png", rot[x0:x0 + band_h])
            t = ocr_buf(buf.tobytes(), server)
            if t:
                textos.append(t)
        _, buf = cv2.imencode(".png", rot[rot.shape[0] - band_h:])
        t = ocr_buf(buf.tobytes(), server)
        if t:
            textos.append(t)
    else:
        _, buf = cv2.imencode(".png", rot)
        textos.append(ocr_buf(buf.tobytes(), server))
    return textos


def crop_bbox(img, bbox, pad_x, pad_y):
    x1, y1, x2, y2 = map(int, bbox)
    h, w = img.shape[:2]
    px, py = int((x2 - x1) * pad_x), int((y2 - y1) * pad_y)
    x1 = max(0, x1 - px)
    x2 = min(w, x2 + px)
    y1 = max(0, y1 - py)
    y2 = min(h, y2 + py)
    return img[y1:y2, x1:x2]


def pick_frames(frames, n):
    """Frames ordenados por tiempo repartidos uniformemente (esquina a puerta)."""
    frames = sorted(frames)
    if len(frames) <= n:
        return frames
    idx = [round(i * (len(frames) - 1) / (n - 1)) for i in range(n)]
    return [frames[i] for i in idx]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run", required=True, help="dir de corrida de rango_extraer")
    p.add_argument("--video", required=True, help="video 4K original")
    p.add_argument("--server", default="http://localhost:5003")
    p.add_argument("--max-crops", type=int, default=16,
                   help="tope de crops OCR por camion")
    args = p.parse_args()

    db = os.path.join(args.run, "procesamiento.db")
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS ocr (
        camion INTEGER, frame INTEGER, conf REAL, texto TEXT,
        endpoint TEXT, ms REAL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ocr_codigos (
        camion INTEGER, frame INTEGER, codigo TEXT, tier TEXT)""")
    cur.execute("DROP TABLE IF EXISTS codigos")
    cur.execute("""CREATE TABLE codigos (
        camion INTEGER, orden INTEGER, codigo TEXT, tier TEXT, peso INTEGER,
        frames TEXT, parciales TEXT, size_type TEXT,
        PRIMARY KEY (camion, orden))""")
    cur.execute("DELETE FROM ocr")
    cur.execute("DELETE FROM ocr_codigos")
    cur.execute("DELETE FROM codigos")
    conn.commit()
    os.makedirs(os.path.join(args.run, "crops"), exist_ok=True)

    rangos = {r[0]: (r[1], r[2]) for r in cur.execute(
        "SELECT idx, inicio, fin FROM rangos")}
    row = cur.execute(
        "SELECT v FROM meta WHERE k='proxy_offset'").fetchone()
    delta = int(row[0]) if row else 0
    cap = cv2.VideoCapture(args.video)
    t0 = time.time()
    for camion, (r0, r1) in sorted(rangos.items()):
        cand = [f for (f,) in cur.execute(
            "SELECT DISTINCT frame FROM dets WHERE cls=3 AND frame BETWEEN ? "
            "AND ? ORDER BY frame", (r0, r1))]
        if not cand:
            print(f"camion {camion}: sin detecciones cls3")
            continue
        frames = pick_frames(cand, max(args.max_crops - 6, 4))
        wanted = set(frames)

        def det_de(f):
            return cur.execute(
                "SELECT x1,y1,x2,y2,conf FROM dets WHERE frame=? AND cls=3 "
                "ORDER BY conf DESC LIMIT 1", (f,)).fetchone()

        cap.set(cv2.CAP_PROP_POS_FRAMES, r0 + delta)
        votos = {}
        votos_lista = []
        crops_voto = {}
        confirmados = []
        seguidos = set()
        textos_camion = []
        tier_votos = []
        parciales_acc = []
        tamanos_acc = []
        crops_hechos = 0
        for f in range(r0, r1 + 1):
            ok, img = cap.read()
            if not ok:
                break
            if f not in wanted or crops_hechos >= args.max_crops:
                continue
            det = det_de(f)
            if det is None:
                continue
            crops_hechos += 1
            x1, y1, x2, y2, conf = det  # bbox normalizado 0-1
            w_img, h_img = img.shape[1], img.shape[0]
            bx1, by1 = int(x1 * w_img), int(y1 * h_img)
            bx2, by2 = int(x2 * w_img), int(y2 * h_img)
            crop = crop_bbox(img, (bx1, by1, bx2, by2), PAD_X, PAD_Y)
            ch, cw = crop.shape[:2]
            if ch < 8 or cw < 8:
                continue
            t1 = time.time()
            if ch > 1.2 * cw:
                ts = ocr_tall(crop, args.server)
                endpoint = "/ocr(rot90)"
            else:
                _, buf = cv2.imencode(".png", crop)
                ts = [ocr_buf(buf.tobytes(), args.server)]
                endpoint = "/ocr"
            # size/type presente sin codigo ISO -> el codigo esta cerca:
            # reintento con padding ampliado (una vez)
            lineas = [l for t in ts for l in ocr_codes.limpiar(t)]
            if (ocr_codes.extraer_tamano_tipo(lineas)
                    and not ocr_codes.extraer_codigos(lineas)):
                crop2 = crop_bbox(img, (bx1, by1, bx2, by2),
                                  PAD_RETRY_X, PAD_RETRY_Y)
                if ch > 1.2 * cw:
                    ts2 = ocr_tall(crop2, args.server)
                else:
                    _, buf = cv2.imencode(".png", crop2)
                    ts2 = [ocr_buf(buf.tobytes(), args.server)]
                ts = ts + ts2
                endpoint += "+pad0.75"
                lineas = [l for t in ts for l in ocr_codes.limpiar(t)]
            ms = (time.time() - t1) * 1000
            texto = " | ".join(t for t in ts if t)
            cur.execute("INSERT INTO ocr VALUES (?,?,?,?,?,?)",
                        (camion, f, conf, texto, endpoint, round(ms)))
            textos_camion.extend(ts)
            crop_path = os.path.join(args.run, "crops", f"c{camion}_f{f}.png")
            cv2.imwrite(crop_path, crop)

            cands = {}
            for code, tier, _ in ocr_codes.extraer_codigos(lineas):
                orden = {"strict": 0, "repaired": 1, "raw": 2}
                if code not in cands or orden[tier] < orden[cands[code]]:
                    cands[code] = tier
            if any(t in ("strict", "repaired") for t in cands.values()):
                cands = {c: t for c, t in cands.items()
                         if t in ("strict", "repaired")}
            for code, tier in cands.items():
                cur.execute("INSERT INTO ocr_codigos VALUES (?,?,?,?)",
                            (camion, f, code, tier))
                if code in confirmados:
                    continue  # lectura repetida de un codigo ya confirmado
                peso = 2 if tier == "strict" else 1
                votos[code] = votos.get(code, 0) + peso
                votos_lista.extend([code] * peso)
                crops_voto.setdefault(code, set()).add(f)
                tier_votos.append((code, tier))
            conn.commit()
            print(f"camion {camion} frame {f} conf {conf:.2f} [{endpoint}] "
                  f"{ms:.0f}ms -> {texto!r}")
            # foto respaldo 4K con el bbox del codigo dibujado
            if cands:
                ev_dir = os.path.join(args.run, "evidencia")
                os.makedirs(ev_dir, exist_ok=True)
                ev = img.copy()
                cv2.rectangle(ev, (bx1, by1), (bx2, by2),
                              (0, 0, 255), 8)
                cv2.putText(ev, " | ".join(cands)[:40],
                            (bx1, max(20, by1 - 25)),
                            cv2.FONT_HERSHEY_SIMPLEX, 2.2, (0, 0, 255), 5)
                ok, buf = cv2.imencode(".jpg", ev,
                                       [cv2.IMWRITE_JPEG_QUALITY, 92])
                if ok:
                    with open(os.path.join(
                            ev_dir, f"c{camion}_f{f}_4k.jpg"), "wb") as fh:
                        fh.write(buf.tobytes())
            # confirmacion por codigo: 2 crops con la MISMA string exacta
            for code in list(crops_voto):
                if code in confirmados:
                    continue
                if len(crops_voto[code]) >= 2:
                    confirmados.append(code)
                    parciales_acc = ocr_codes.extraer_partiales(
                        [l for t in textos_camion
                         for l in ocr_codes.limpiar(t)])
                    tamanos_acc = ocr_codes.extraer_tamano_tipo(
                        [l for t in textos_camion
                         for l in ocr_codes.limpiar(t)])
                    orden = len(confirmados) - 1
                    tiers = ",".join(sorted(
                        {t for c, t in tier_votos if c == code}))
                    frames_cod = sorted(crops_voto[code])
                    cur.execute(
                        "INSERT OR REPLACE INTO codigos VALUES (?,?,?,?,?,?,?,?)",
                        (camion, orden, code, tiers, votos[code],
                         ",".join(map(str, frames_cod)),
                         ",".join(parciales_acc),
                         ",".join(f"{c}={d}" for c, d in tamanos_acc)))
                    conn.commit()
                    print(f"  >> camion {camion} CODIGO CONFIRMADO #{orden}: "
                          f"{code} (peso={votos[code]} crops={frames_cod})")
            # seguimiento: codigo nuevo con 1 solo crop -> muestrear los frames
            # siguientes para confirmarlo (etiqueta visible ~2-3s)
            for code in crops_voto:
                if code in confirmados or code in seguidos:
                    continue
                if len(crops_voto[code]) == 1:
                    seguidos.add(code)
                    for df in (5, 10, 15):
                        if r0 <= f + df <= r1:
                            wanted.add(f + df)

        lineas = [l for t in textos_camion for l in ocr_codes.limpiar(t)]
        parciales = ocr_codes.extraer_partiales(lineas)
        tamanos = ocr_codes.extraer_tamano_tipo(lineas)
        # codigos con votos pero no confirmados (1 crop, o vecinos fuzzy):
        # agrupar por vecindario (dist<=2) y guardar el ganador como fila
        pendientes = [c for c in crops_voto if c not in confirmados]
        grupos = []
        for code in pendientes:
            for g in grupos:
                if any(ocr_codes._levenshtein(code, c) <= 2 for c in g):
                    g.append(code)
                    break
            else:
                grupos.append([code])
        orden = len(confirmados)
        for g in grupos:
            best = max(g, key=lambda c: (votos[c], len(crops_voto[c])))
            crops_g = sorted({f for c in g for f in crops_voto[c]})
            tiers = ",".join(sorted({t for c, t in tier_votos if c in g}))
            cur.execute(
                "INSERT OR REPLACE INTO codigos VALUES (?,?,?,?,?,?,?,?)",
                (camion, orden, best,
                 tiers if len(crops_g) >= 2 else tiers + ",noconfirmado",
                 sum(votos[c] for c in g), ",".join(map(str, crops_g)),
                 ",".join(parciales), ",".join(f"{c}={d}" for c, d in tamanos)))
            orden += 1
        conn.commit()
        print(f"  >> camion {camion}: parciales={parciales}")
        if tamanos:
            print(f"  >> camion {camion} size/type: "
                  + ", ".join(f"{c}={d}" for c, d in tamanos))
        filas = [r for r in cur.execute(
            "SELECT codigo, tier, peso, frames FROM codigos WHERE camion=? "
            "ORDER BY orden", (camion,))]
        for cod, tier, peso, fr in filas:
            marca = "CONFIRMADO" if "noconfirmado" not in tier else "no-confirmado"
            print(f"  >> camion {camion} [{marca}] {cod} "
                  f"(tier={tier} peso={peso} frames={fr})")
        print(f"  >> camion {camion} FINAL: {filas if filas else 'SIN CODIGO'}")

    cap.release()
    print(f"\nOCR total: {time.time() - t0:.1f}s | tablas ocr/ocr_codigos/codigos en {db}")


if __name__ == "__main__":
    main()
