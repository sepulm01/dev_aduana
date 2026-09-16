#!/usr/bin/env python3
"""Pipeline de extraccion en 2 fases, sin MOG2:

Fase 1 - prospeccion: YOLO (clase truck) cada --paso frames + muestras finas
        (--fine) dentro de grupos positivos. Las transiciones neg->pos y
        pos->neg se afinan con bisectriz -> rangos [entrada, salida] por
        camion (sin solapes).
Fase 2 - barrido fino: YOLO frame a frame DENTRO de cada rango (saltando
        frames ya inferidos). TODA deteccion (cls, conf, bbox) queda en
        SQLite (tabla dets) - nunca se re-infiere un frame.

Seleccion final: por camion y por clase, los top-N frames por confianza
se guardan como JPEG 4K calidad 95 (material para OCR futuro), registrados
en la tabla mejores.
"""
import argparse
import json
import os
import sqlite3
import time

import cv2
import numpy as np

from yolo_gate import YoloGate

CLASE_TRUCK = 4


def calibrar_offset(proxy_path, video_path):
    """Offset temporal proxy->original (proxy[f] ~ original[f+delta]).

    Se mide comparando pixeles en 3 muestras y se cachea en .cal.json
    junto al proxy (el offset es propio del transcode, no del video).
    """
    cal = proxy_path + ".cal.json"
    if os.path.exists(cal):
        try:
            return int(json.load(open(cal))["delta"])
        except Exception:
            pass
    cap_p = cv2.VideoCapture(proxy_path)
    cap_o = cv2.VideoCapture(video_path)
    total = int(cap_o.get(cv2.CAP_PROP_FRAME_COUNT))
    deltas = []
    # Se rechazan muestras ambiguas (escenas estaticas: frames contiguos
    # casi identicos) exigiendo margen entre el mejor y el segundo mejor.
    for frac in (0.1, 0.25, 0.4, 0.55, 0.7, 0.85, 0.95):
        f = min(int(total * frac), total - 8)
        cap_p.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, p = cap_p.read()
        if not ok:
            continue
        diffs = []
        for df in range(-2, 3):
            cap_o.set(cv2.CAP_PROP_POS_FRAMES, f + df)
            ok, o = cap_o.read()
            if not ok:
                continue
            o = cv2.resize(o, (p.shape[1], p.shape[0]),
                           interpolation=cv2.INTER_AREA)
            diffs.append((df, float(np.abs(
                p.astype(np.float32) - o.astype(np.float32)).mean())))
        orden = sorted(diffs, key=lambda t: t[1])
        if len(orden) >= 2 and orden[1][1] - orden[0][1] > 0.4:
            deltas.append(orden[0][0])
    delta = int(np.median(deltas)) if deltas else 0
    with open(cal, "w") as fh:
        json.dump({"delta": delta, "muestras": deltas}, fh)
    cap_p.release()
    cap_o.release()
    return delta


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video", required=True)
    p.add_argument("--output", default="frames_extraidos")
    p.add_argument("--paso", type=int, default=100, help="intervalo del barrido grueso")
    p.add_argument("--fine", type=int, default=20, help="paso de muestras finas en grupos positivos")
    p.add_argument("--top", type=int, default=3, help="top-N frames por clase por camion")
    p.add_argument("--conf", type=float, default=0.40)
    p.add_argument("--min-frames", type=int, default=20,
                   help="rango minimo para considerarlo camion (frames)")
    p.add_argument("--model", default=None)
    p.add_argument("--imgsz", type=int, default=640,
                   help="resolucion de inferencia YOLO (640: rapido, 1280: maxima precision)")
    p.add_argument("--batch", type=int, default=8,
                   help="tamano de lote para inferencia (amortiza overhead de GPU)")
    p.add_argument("--proxy-height", type=int, default=540,
                   help="alto del proxy de baja resolucion para fases 1-2 (0 = sin proxy). "
                        "El 4K solo se lee para guardar los top-N.")
    p.add_argument("--refinar", action="store_true",
                   help="Fase 2 con muestreo multiresolucion + bisectriz por clase")
    p.add_argument("--paso-ref", type=int, default=10,
                   help="paso del muestreo fino en modo --refinar")
    p.add_argument("--paso-ocr", type=int, default=5,
                   help="paso de cosecha densa de detecciones dentro de rangos "
                        "para el OCR (0 = desactivada)")
    p.add_argument("--w", type=int, default=5,
                   help="ventana densa alrededor del maximo de conf (--refinar)")
    args = p.parse_args()

    os.makedirs(args.output, exist_ok=True)
    db = os.path.join(args.output, "procesamiento.db")
    for suf in ("", "-wal", "-shm"):
        try:
            os.remove(db + suf)
        except FileNotFoundError:
            pass
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE dets (
        frame INTEGER, cls INTEGER, conf REAL,
        x1 REAL, y1 REAL, x2 REAL, y2 REAL)""")
    cur.execute("""CREATE TABLE mejores (
        camion INTEGER, cls INTEGER, conf REAL, frame INTEGER,
        archivo TEXT)""")
    cur.execute("""CREATE TABLE runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        elapsed_s REAL, yolo_llamadas INTEGER, rangos INTEGER,
        frames_barridos INTEGER, guardados INTEGER)""")
    cur.execute("""CREATE TABLE rangos (
        idx INTEGER, inicio INTEGER, fin INTEGER)""")
    cur.execute("CREATE INDEX idx_dets_cls_conf ON dets (cls, conf)")
    cur.execute("CREATE INDEX idx_dets_frame ON dets (frame)")
    conn.commit()

    t_inicio = time.time()
    gate = YoloGate(model_path=args.model, conf_thres=args.conf, imgsz=args.imgsz)
    gate.warmup()

    # Proxy de baja resolucion para fases 1-2 (transcode una vez, reutilizable)
    cap = None
    if args.proxy_height > 0:
        proxy_dir = os.path.join(os.path.dirname(os.path.abspath(args.video)), ".proxy")
        os.makedirs(proxy_dir, exist_ok=True)
        proxy_path = os.path.join(
            proxy_dir,
            os.path.splitext(os.path.basename(args.video))[0] +
            f"_h{args.proxy_height}.mp4")
        nuevo = not os.path.exists(proxy_path)
        if nuevo:
            import subprocess
            subprocess.run(
                ["ffmpeg", "-y", "-i", args.video, "-vf",
                 f"scale=-2:{args.proxy_height}",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
                 "-an", proxy_path],
                check=True, capture_output=True)
            print(f"proxy creado: {proxy_path}")
        else:
            pass
        cap = cv2.VideoCapture(proxy_path)
        delta = calibrar_offset(proxy_path, args.video)
        print(f"proxy: {proxy_path} (offset {delta:+d} frames)")
        cur.execute("CREATE TABLE IF NOT EXISTS meta "
                    "(k TEXT PRIMARY KEY, v TEXT)")
        cur.execute("INSERT OR REPLACE INTO meta VALUES ('proxy_offset', ?)",
                    (str(delta),))
        conn.commit()
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
        h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        print(f"proxy {w:.0f}x{h:.0f} (bbox normalizado 0-1)")
    else:
        cap = cv2.VideoCapture(args.video)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
        h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)

    inferidos = set()

    def lee(f):
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, frame = cap.read()
        return frame if ok else None

    def tiene_truck(f):
        if f in inferidos:
            return None  # ya inferido: sin nueva llamada (mismo signo conocido)
        frame = lee(f)
        if frame is None:
            return False
        _, dets = gate.has_class(frame)
        for d in dets:
            cur.execute("INSERT INTO dets VALUES (?,?,?,?,?,?,?)",
                        (f, int(d[5]), float(d[4]),
                         float(d[0]) / w, float(d[1]) / h,
                         float(d[2]) / w, float(d[3]) / h))
        conn.commit()
        inferidos.add(f)
        return any(int(d[5]) == CLASE_TRUCK for d in dets)

    # ================= FASE 1: muestras + transiciones + bisectriz =================
    puntos = {}
    for f in range(0, total, args.paso):
        puntos[f] = tiene_truck(f)

    # muestras finas dentro de grupos positivos del barrido grueso
    muestras = sorted((f for f in range(0, total, args.paso)), key=int)
    i = 0
    while i < len(muestras):
        if not puntos.get(muestras[i], False):
            i += 1
            continue
        j = i
        while j + 1 < len(muestras) and puntos.get(muestras[j + 1], False):
            j += 1
        for f in range(muestras[i] + args.fine, muestras[j], args.fine):
            if f not in puntos:
                puntos[f] = tiene_truck(f)
        i = j + 1

    puntos[total] = False  # cierre virtual al final del video
    orden = sorted(puntos.items())

    rangos = []
    pend_entrada = None
    for k in range(1, len(orden)):
        (f0, p0), (f1, p1) = orden[k - 1], orden[k]
        if p0 == p1:
            continue
        lo, hi = f0, f1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if tiene_truck(mid):
                if p1:
                    hi = mid
                else:
                    lo = mid
            else:
                if p1:
                    lo = mid
                else:
                    hi = mid
        if p1:  # neg -> pos: entrada
            pend_entrada = hi
        else:   # pos -> neg: salida
            if pend_entrada is not None:
                if lo - pend_entrada >= args.min_frames:
                    rangos.append((pend_entrada, lo))
                pend_entrada = None

    print(f"rangos detectados: {len(rangos)}  {rangos}")
    for idx, (a, b) in enumerate(rangos, 1):
        cur.execute("INSERT INTO rangos VALUES (?,?,?)", (idx, a, b))
    conn.commit()
    print(f"[FASE1 {time.time() - t_inicio:.1f}s]")

    # ================= FASE 2: barrido dentro de rangos =================
    frames_barridos = 0
    f_actual = [0]

    def dets_de_frame_imagen(frame):
        """YOLO sobre un frame ya decodificado y guarda todo en DB."""
        _, dets = gate.has_class(frame)
        for d in dets:
            cur.execute("INSERT INTO dets VALUES (?,?,?,?,?,?,?)",
                        (f_actual[0], int(d[5]), float(d[4]),
                         float(d[0]) / w, float(d[1]) / h,
                         float(d[2]) / w, float(d[3]) / h))
        return [(int(d[5]), float(d[4])) for d in dets]

    if args.refinar:
        # Muestreo multiresolucion + bisectriz por clase, con inferencia
        # por lotes (las muestras y las ventanas densas se batean).
        for r0, r1 in rangos:
            cap.set(cv2.CAP_PROP_POS_FRAMES, r0)
            cache = []
            for f in range(r0, r1 + 1):
                ok, frame = cap.read()
                if not ok:
                    break
                cache.append(frame)

            def batch_infer(frames_list):
                pend = sorted({f for f in frames_list if f not in inferidos})
                for i in range(0, len(pend), args.batch):
                    chunk = pend[i:i + args.batch]
                    imgs = [cache[f - r0] for f in chunk
                            if 0 <= f - r0 < len(cache)]
                    if not imgs:
                        continue
                    for f, dets in zip(chunk[:len(imgs)],
                                       gate.detect_batch(imgs)):
                        f_actual[0] = f
                        for d in dets:
                            cur.execute("INSERT INTO dets VALUES (?,?,?,?,?,?,?)",
                                        (f, int(d[5]), float(d[4]),
                                         float(d[0]) / w, float(d[1]) / h,
                                         float(d[2]) / w, float(d[3]) / h))
                        inferidos.add(f)
                    conn.commit()

            def signos_de(f):
                if f not in inferidos:
                    return []
                return cur.execute(
                    "SELECT cls, conf FROM dets WHERE frame=?", (f,)).fetchall()

            def dets_frame(f):
                if f not in inferidos:
                    f_actual[0] = f
                    idx = f - r0
                    if not (0 <= idx < len(cache)):
                        return []
                    result = dets_de_frame_imagen(cache[idx])
                    conn.commit()
                    inferidos.add(f)
                    return result
                return signos_de(f)

            def tiene_clase(f, cls):
                return any(c == cls for c, _ in dets_frame(f))

            fs = list(range(r0, r1 + 1, args.paso_ref))
            if fs[-1] != r1:
                fs.append(r1)
            batch_infer(fs)
            signos = {f: signos_de(f) for f in fs}
            ventanas = []
            for cls in range(5):
                presentes = [f for f in fs if any(c == cls for c, _ in signos[f])]
                if not presentes:
                    continue
                # intervalos contiguos (gap <= 2*paso se considera el mismo)
                inter = []
                a = b = presentes[0]
                for f in presentes[1:]:
                    if f - b <= 2 * args.paso_ref:
                        b = f
                    else:
                        inter.append((a, b))
                        a = b = f
                inter.append((a, b))

                for a, b in inter:
                    idx_a = fs.index(a)
                    lo = fs[idx_a - 1] if idx_a > 0 else r0
                    hi = a
                    while hi - lo > 1:
                        mid = (lo + hi) // 2
                        if tiene_clase(mid, cls):
                            hi = mid
                        else:
                            lo = mid
                    entrada = hi
                    idx_b = fs.index(b)
                    lo = b
                    hi = fs[idx_b + 1] if idx_b + 1 < len(fs) else r1 + 1
                    while hi - lo > 1:
                        mid = (lo + hi) // 2
                        if tiene_clase(mid, cls):
                            lo = mid
                        else:
                            hi = mid
                    salida = lo
                    fmax = max((f for f in presentes if a <= f <= b),
                               key=lambda f: max((conf for c, conf in signos[f]
                                                  if c == cls), default=0))
                    for f in range(max(entrada, fmax - args.w),
                                   min(salida, fmax + args.w) + 1):
                        ventanas.append(f)
            batch_infer(ventanas)
            if args.paso_ocr:
                extra = [f for f in range(r0, r1 + 1, args.paso_ocr)
                         if f not in inferidos]
                batch_infer(extra)
            conn.commit()
    else:
        # Barrido completo con inferencia POR LOTES (amortiza overhead GPU)
        for r0, r1 in rangos:
            cap.set(cv2.CAP_PROP_POS_FRAMES, r0)
            bframes, bids = [], []

            def flush():
                if not bframes:
                    return
                for f, dets in zip(bids, gate.detect_batch(bframes)):
                    for d in dets:
                        cur.execute("INSERT INTO dets VALUES (?,?,?,?,?,?,?)",
                                    (f, int(d[5]), float(d[4]),
                                     float(d[0]) / w, float(d[1]) / h,
                                     float(d[2]) / w, float(d[3]) / h))
                    inferidos.add(f)
                conn.commit()
                bframes.clear()
                bids.clear()

            for f in range(r0, r1 + 1):
                ok, frame = cap.read()
                if not ok:
                    break
                if f in inferidos:
                    continue
                bframes.append(frame)
                bids.append(f)
                frames_barridos += 1
                if len(bframes) >= args.batch:
                    flush()
            flush()

    print(f"[FASE2 {time.time()-t_inicio:.1f}s]")
    # ================= Seleccion top-N por clase por camion =================
    # Se lee el video ORIGINAL (4K) en streaming secuencial por rango:
    # cero seeks por frame.
    print(f"[SELECCION {time.time() - t_inicio:.1f}s]")
    seleccion = {}
    for camion, (r0, r1) in enumerate(rangos, 1):
        for cls in range(5):
            rows = cur.execute(
                "SELECT frame, conf FROM dets WHERE cls=? AND frame BETWEEN ? AND ? "
                "ORDER BY conf DESC LIMIT ?", (cls, r0, r1, args.top)).fetchall()
            for frame, conf in rows:
                if frame not in seleccion or seleccion[frame][2] < conf:
                    seleccion[frame] = (camion, cls, conf)

    cap4k = cv2.VideoCapture(args.video)
    guardados = 0
    escritos = set()
    for r0, r1 in rangos:
        cap4k.set(cv2.CAP_PROP_POS_FRAMES, r0 + delta)
        for f in range(r0, r1 + 1):
            ok, frame4k = cap4k.read()
            if not ok:
                break
            if f in seleccion:
                nombre = f"processed_{f:06d}.jpg"
                ok, buf = cv2.imencode(".jpg", frame4k,
                                       [cv2.IMWRITE_JPEG_QUALITY, 95])
                if ok:
                    with open(os.path.join(args.output, nombre), "wb") as fh:
                        fh.write(buf.tobytes())
                    escritos.add(f)
                    guardados += 1
    cap4k.release()

    for frame, (camion, cls, conf) in sorted(seleccion.items()):
        cur.execute("INSERT INTO mejores VALUES (?,?,?,?,?)",
                    (camion, cls, conf, frame, f"processed_{frame:06d}.jpg"))

    print(f"[SELECCION {time.time()-t_inicio:.1f}s]")
    elapsed = time.time() - t_inicio
    cur.execute(
        "INSERT INTO runs (elapsed_s, yolo_llamadas, rangos, frames_barridos, guardados) "
        "VALUES (?,?,?,?,?)",
        (round(elapsed, 1), gate.calls, len(rangos), frames_barridos, guardados))
    conn.commit()
    conn.close()
    cap.release()
    cap4k.release()

    print(f"frames barridos en Fase 2: {frames_barridos}")
    print(f"guardados: {guardados} JPEG 4K q95")
    print(f"YOLO llamadas: {gate.calls} | tiempo: {elapsed:.1f}s")


if __name__ == "__main__":
    main()
