#!/usr/bin/env python3
"""Captor alt_batch3: evidencia cruda por camara, sin agrupar ni cortar.

- ffmpeg hwaccel cuda decodifica el RTSP a 4K (o un video local de test)
  y alimenta una ventana deslizante con wall-clock por frame.
- YOLO espaciado (1/s) -> denso (cada --cada-denso) con rewind y filtro
  de direccion (cam1: izq->der valido; cam2: sin filtro).
- Cada deteccion cls3 emite un registro {"tipo":"det", ...} con bbox,
  area, conf, color HSV y (si supera --umbral-area) su crop 4K al OCR.
- El OCR (hilo aparte) emite {"tipo":"ocr", ...} asociado por seq.
- El "run" (silencio de SILENCIO_DENSO) solo decide cuando guardar los
  frames 4K completos: top-K crops de codigo ({"tipo":"frame4k"}) y
  top-K frames con mas sellos + pose kpt3 ({"tipo":"pico"}).
- NINGUN registro implica una unidad de camion: agrupar es trabajo del
  analizador.

Uso: python captor.py --camara 1 --rtsp rtsp://... \
       [--raw raw_cam1.jsonl] [--video test.mkv] [--sin-ocr] [--gui]
"""
import argparse
import json
import os
import queue
import statistics
import subprocess
import sys
import threading
import time

import cv2
import numpy as np
import requests

import ocr_codes
from pose_gate import PoseGate
from video import (PAD_X, PAD_Y, SILENCIO_DENSO, SERVER, Ventana,
                   _sello_en_kpt3, decodificar_cmd, leer_frames, sonda_hw)
from yolo_gate import YoloGate

W, H = 3840, 2160
VENTANA_4K = 40
PAD_AREA = 1500.0
K = 2


def _hsv_promedio(crop):
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    v = hsv[:, :, 2]
    mask = (v > 0.15 * 255) & (v < 0.95 * 255)
    if not mask.any():
        return None
    h, s, val = (hsv[:, :, 0][mask].mean() / 179.0,
                 hsv[:, :, 1][mask].mean() / 255.0,
                 v[mask].mean() / 255.0)
    return [round(float(h), 3), round(float(s), 3), round(float(val), 3)]


def _guardar_jpg(img, path, calidad=85):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, calidad])
    if ok:
        with open(path, "wb") as fh:
            fh.write(buf.tobytes())
    return ok


def _leer_rtsp(url, ventana, W, H, detener, proc_holder):
    frame_bytes = W * H * 3
    idx = 0
    backoff = 2.0
    while not detener["on"]:
        cmd = ["ffmpeg", "-v", "error",
               "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
               "-rtsp_transport", "tcp", "-timeout", "5000000",
               "-i", url,
               "-vf", "hwdownload,format=nv12",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        proc_holder["proc"] = proc
        print(f"[rtsp] conectado (f{idx})", flush=True)
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            ventana.poner(idx, np.frombuffer(buf, np.uint8).reshape(H, W, 3),
                          time.time())
            idx += 1
        proc.terminate()
        proc_holder["proc"] = None
        if detener["on"]:
            break
        print(f"[rtsp] corte en f{idx}, reintento en {backoff:.0f}s",
              flush=True)
        time.sleep(backoff)
        backoff = min(backoff * 2, 15.0)
    ventana.terminar()


def _leer_video(path, ventana, W, H, detener, proc_holder):
    hw = sonda_hw(path)
    cmd = decodificar_cmd(path, W, H, hw)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    proc_holder["proc"] = proc
    leer_frames(proc, ventana, W * H * 3, W, H)


def worker_ocr(ctx, args):
    while True:
        item = ctx["q"].get()
        try:
            if item is None:
                if ctx["cerrar"]:
                    return
                continue
            seq, fpos, area, png = item
            t1 = time.time()
            try:
                r = requests.post(args.server + "/ocr",
                                  files={"file": png}, timeout=180)
                r.raise_for_status()
                texto = r.json().get("text", "")
            except requests.RequestException as e:
                print(f"[ocr s{seq}] f{fpos} ERROR {e}", flush=True)
                continue
            ms = (time.time() - t1) * 1000
            cods = ocr_codes.extraer_codigos(ocr_codes.limpiar(texto))
            rec = {"tipo": "ocr", "cam": args.camara, "seq": seq,
                   "f": fpos, "texto": texto,
                   "codigos": [[c, t] for c, t, _ in cods],
                   "ms": round(ms)}
            with ctx["lock"]:
                with open(args.raw, "a") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if cods:
                print(f"[ocr s{seq}] f{fpos} a={area:.0f} {ms:.0f}ms -> "
                      + ", ".join(f"{c}({t})" for c, t, _ in cods),
                      flush=True)
            else:
                print(f"[ocr s{seq}] f{fpos} a={area:.0f} sin codigo "
                      f"<- {texto!r}", flush=True)
        finally:
            ctx["q"].task_done()


def procesar_camara(args, gate, gate_pose, ctx):
    camara = args.camara
    filtrar_dir = camara == 1
    umbral_eq = args.umbral_area or (3000 if camara == 1 else 2000)
    umbral_ef = umbral_eq * (W * H) / (960 * 540)
    paso_espaciado = max(1, int(round(args.fps)))
    retro = paso_espaciado // 2

    os.makedirs(os.path.dirname(args.raw) or ".", exist_ok=True)
    os.makedirs(f"crops_b3/cam{camara}", exist_ok=True)
    os.makedirs("fotos_b3", exist_ok=True)

    detener = {"on": False}
    proc_holder = {"proc": None}
    ventana = Ventana(VENTANA_4K)
    if args.video:
        lector = threading.Thread(
            target=_leer_video,
            args=(args.video, ventana, W, H, detener, proc_holder),
            daemon=True)
    else:
        lector = threading.Thread(
            target=_leer_rtsp,
            args=(args.rtsp, ventana, W, H, detener, proc_holder),
            daemon=True)
    lector.start()

    pos = 0
    modo = "espaciado"
    sin_interes = 0
    dir_dx = []
    valida = None
    pendientes = []
    run_id = 0
    best_code = []
    tops_sellos = []
    ts_frame = {}
    n_dets_run = 0
    n_sellos_run = 0
    n_ocr_run = 0

    def append_rec(rec):
        with ctx["lock"]:
            with open(args.raw, "a") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def next_seq():
        with ctx["lock"]:
            ctx["seq"] += 1
            return ctx["seq"]

    def flush_pendientes():
        nonlocal pendientes, n_ocr_run
        for rec, png, crop in pendientes:
            seq = next_seq()
            rec["seq"] = seq
            if png is not None:
                path = os.path.join(
                    "crops_b3", f"cam{camara}",
                    f"s{seq:08d}_f{rec['f']:06d}_a{int(rec['area']):06d}.jpg")
                ok_, buf = cv2.imencode(".jpg", crop,
                                        [cv2.IMWRITE_JPEG_QUALITY, 90])
                if ok_:
                    with open(path, "wb") as fh:
                        fh.write(buf.tobytes())
                rec["crop"] = path
                n_ocr_run += 1
                if not args.sin_ocr:
                    try:
                        ctx["q"].put_nowait(
                            (seq, rec["f"], rec["area"], png))
                    except queue.Full:
                        pass
            append_rec(rec)
        pendientes = []

    def cerrar_run():
        nonlocal best_code, tops_sellos, n_dets_run, n_sellos_run, n_ocr_run
        tops_fs = {p[1] for p in tops_sellos}
        if n_dets_run >= 5 or n_ocr_run >= 1:
            for area, fpos, img in best_code:
                if fpos in tops_fs:
                    continue
                seq = next_seq()
                path = os.path.join(
                    "fotos_b3",
                    f"cam{camara}_f4k_{seq:08d}_f{fpos:06d}.jpg")
                if _guardar_jpg(img, path, 72):
                    append_rec({"tipo": "frame4k", "cam": camara, "seq": seq,
                                "f": fpos,
                                "ts": round(ts_frame.get(fpos, 0.0), 2),
                                "area": round(area), "path": path,
                                "run": run_id})
        if n_sellos_run >= 6:
            for n, fpos, img, dets_s in tops_sellos:
                r = _sello_en_kpt3(img, dets_s, gate_pose, W, H)
                anot = img.copy()
                for d in dets_s:
                    x1, y1, x2, y2 = [int(v) for v in d["bbox"]]
                    color = (0, 255, 255) if d["cls"] == 0 else (255, 0, 255)
                    cv2.rectangle(anot, (x1, y1), (x2, y2), color, 3)
                kpt = r.get("kpt3")
                if kpt:
                    kx, ky = int(kpt["x"] * W), int(kpt["y"] * H)
                    cv2.circle(anot, (kx, ky), 40, (0, 255, 0), 6)
                seq = next_seq()
                foto = os.path.join(
                    "fotos_b3",
                    f"cam{camara}_p{seq:08d}_f{fpos:06d}.jpg")
                guardada = _guardar_jpg(anot, foto, 72)
                sc = None
                if kpt:
                    cx, cy = kpt["x"] * W, kpt["y"] * H
                    half = 0.07 * W
                    for d in dets_s:
                        x1, y1, x2, y2 = d["bbox"]
                        bw, bh = x2 - x1, y2 - y1
                        bx, by = (x1 + x2) / 2, (y1 + y2) / 2
                        if abs(bx - cx) <= 2.5 * bw and \
                                abs(by - cy) <= 2.5 * bh:
                            half = max(half, 2.2 * bw, 2.2 * bh)
                    x1, y1 = max(0, int(cx - half)), max(0, int(cy - half))
                    x2, y2 = min(W, int(cx + half)), min(H, int(cy + half))
                    crop = img[y1:y2, x1:x2]
                    if crop.shape[0] >= 60 and crop.shape[1] >= 60:
                        sc = f"{foto[:-4]}_sello.jpg"
                        _guardar_jpg(crop, sc, 90)
                append_rec({"tipo": "pico", "cam": camara, "seq": seq,
                            "f": fpos,
                            "ts": round(ts_frame.get(fpos, 0.0), 2),
                            "run": run_id, "n_sellos": n,
                            "dets": [{"cls": d["cls"],
                                      "conf": round(d["conf"], 3),
                                      "bbox": [round(v, 1)
                                               for v in d["bbox"]]}
                                     for d in dets_s],
                            "kpt3": kpt, "cls": r.get("cls"),
                            "conf": r.get("conf"),
                            "foto": foto if guardada else None,
                            "sello_crop": sc})
        if n_dets_run < 5 and n_ocr_run < 1 and n_sellos_run < 6:
            print(f"[run r{run_id}] sin emision (dets={n_dets_run} "
                  f"sellos={n_sellos_run} ocr={n_ocr_run})", flush=True)
        best_code = []
        tops_sellos = []
        n_dets_run = 0
        n_sellos_run = 0
        n_ocr_run = 0

    try:
        while True:
            res = ventana.obtener(pos)
            if res is None:
                break
            frame, ts = res
            if ts is None:
                ts = time.time()
            ts_frame[pos] = ts
            if len(ts_frame) > 20000:
                for k in [k for k in ts_frame if k < pos - 20000]:
                    ts_frame.pop(k, None)
            hacer = (modo == "denso" and pos % args.cada_denso == 0) or \
                    (modo == "espaciado" and pos % paso_espaciado == 0)
            interes = cls3 = sellos = []
            if hacer:
                dets = gate.detect(frame)
                interes = [d for d in dets if int(d[5]) in (0, 1, 3)]
                cls3 = [d for d in interes if int(d[5]) == 3]
                sellos = [d for d in interes if int(d[5]) in (0, 1)]

            if modo == "espaciado":
                if interes:
                    print(f"[yolo] f{pos} interes -> denso (rewind {retro})",
                          flush=True)
                    modo = "denso"
                    pos = max(0, pos - retro)
                    sin_interes = 0
                    dir_dx = []
                    valida = None if filtrar_dir else True
                    pendientes = []
                    best_code = []
                    tops_sellos = []
                    run_id += 1
                    n_dets_run = 0
                    n_sellos_run = 0
                    n_ocr_run = 0
                    ventana.liberar_hasta(max(0, pos - retro))
                    continue
                pos += 1
            else:
                descartada_ahora = False
                if hacer:
                    if interes:
                        sin_interes = 0
                        mejor = max(interes, key=lambda d: d[4])
                        n_dets_run += len(cls3)
                        n_sellos_run += len(sellos)
                        for d in cls3:
                            x1, y1, x2, y2 = map(float, d[:4])
                            conf = float(d[4])
                            area = (x2 - x1) * (y2 - y1)
                            px, py = int((x2 - x1) * PAD_X), \
                                int((y2 - y1) * PAD_Y)
                            cx1 = max(0, int(x1) - px)
                            cx2 = min(W, int(x2) + px)
                            cy1 = max(0, int(y1) - py)
                            cy2 = min(H, int(y2) + py)
                            crop = frame[cy1:cy2, cx1:cx2]
                            rec = {"tipo": "det", "cam": camara,
                                   "f": pos, "ts": round(ts, 2),
                                   "run": run_id,
                                   "bbox": [round(x1, 1), round(y1, 1),
                                            round(x2, 1), round(y2, 1)],
                                   "area": round(area),
                                   "conf": round(conf, 3),
                                   "cx": round((x1 + x2) / 2, 1),
                                   "cy": round((y1 + y2) / 2, 1),
                                   "hsv": _hsv_promedio(crop),
                                   "crop": None}
                            png = None
                            crop_guard = None
                            if area >= umbral_ef:
                                ok_, buf = cv2.imencode(".png", crop)
                                if ok_:
                                    png = buf.tobytes()
                                    crop_guard = crop
                            pendientes.append((rec, png, crop_guard))
                            if len(best_code) < K:
                                best_code.append((area, pos, frame.copy()))
                            elif area > best_code[0][0]:
                                best_code[0] = (area, pos, frame.copy())
                            best_code.sort(key=lambda x: -x[0])
                        if sellos:
                            dets_s = [{"cls": int(d[5]),
                                       "conf": float(d[4]),
                                       "bbox": [float(d[0]), float(d[1]),
                                                float(d[2]), float(d[3])]}
                                      for d in sellos]
                            tops_sellos.append((len(sellos), pos,
                                                frame.copy(), dets_s))
                            tops_sellos.sort(key=lambda x: -x[0])
                            tops_sellos = tops_sellos[:K]
                        dir_dx.append((float(mejor[0]) + float(mejor[2])) / 2)
                        if valida is None and len(dir_dx) >= 4:
                            med = statistics.median(
                                [dir_dx[i + 1] - dir_dx[i]
                                 for i in range(len(dir_dx) - 1)])
                            if abs(med) > 0.5 or len(dir_dx) >= 6:
                                valida = med > 0.5
                                if valida:
                                    print(f"[dir] f{pos} izq->der VALIDA",
                                          flush=True)
                                    flush_pendientes()
                                else:
                                    print(f"[dir] f{pos} der->izq "
                                          "DESCARTADA", flush=True)
                                    pendientes = []
                                    best_code = []
                                    tops_sellos = []
                                    n_dets_run = 0
                                    n_sellos_run = 0
                                    n_ocr_run = 0
                                    modo = "espaciado"
                                    descartada_ahora = True
                        elif valida is True:
                            flush_pendientes()
                    else:
                        sin_interes += args.cada_denso
                        if sin_interes >= SILENCIO_DENSO:
                            print(f"[yolo] f{pos} silencio -> espaciado",
                                  flush=True)
                            modo = "espaciado"
                            if valida is not False:
                                flush_pendientes()
                                cerrar_run()
                            else:
                                pendientes = []
                pos += paso_espaciado * 2 if descartada_ahora else 1

            ventana.liberar_hasta(max(0, pos - retro))
            if args.gui:
                cv2.imshow(f"cam{camara}", frame)
                if cv2.waitKey(1) == ord("q"):
                    break
    except KeyboardInterrupt:
        print("interrumpido", flush=True)
    finally:
        detener["on"] = True
        ventana.terminar()
        if proc_holder["proc"] is not None:
            proc_holder["proc"].terminate()
        lector.join(timeout=15)
        cv2.destroyAllWindows()


def main():
    global W, H
    sys.stdout.reconfigure(line_buffering=True)
    p = argparse.ArgumentParser()
    p.add_argument("--camara", type=int, required=True, choices=[1, 2])
    p.add_argument("--rtsp", default="")
    p.add_argument("--video", default="",
                   help="video local de test (ignora --rtsp)")
    p.add_argument("--raw", default="")
    p.add_argument("--tamano", default=f"{W}x{H}",
                   help="resolucion de la fuente (ancho x alto)")
    p.add_argument("--umbral-area", type=float, default=None)
    p.add_argument("--cada-denso", type=int, default=4)
    p.add_argument("--conf", type=float, default=0.40)
    p.add_argument("--fps", type=float, default=20.0)
    p.add_argument("--server", default=SERVER)
    p.add_argument("--sin-ocr", action="store_true")
    p.add_argument("--gui", action="store_true")
    args = p.parse_args()
    W, H = map(int, args.tamano.split("x"))
    if not args.raw:
        args.raw = os.path.join("raw", f"cam{args.camara}.jsonl")
    if not args.rtsp and not args.video:
        p.error("--rtsp o --video requerido")

    gate = YoloGate(model_path=None, conf_thres=args.conf, imgsz=640)
    gate.warmup()
    gate_pose = PoseGate()
    gate_pose.warmup()
    ctx = {"q": queue.Queue(maxsize=64), "cerrar": False, "seq": 0,
           "lock": threading.Lock()}
    ocr_t = threading.Thread(target=worker_ocr, args=(ctx, args), daemon=True)
    ocr_t.start()

    try:
        procesar_camara(args, gate, gate_pose, ctx)
    except KeyboardInterrupt:
        pass
    ctx["cerrar"] = True
    ctx["q"].put(None)
    ocr_t.join(timeout=30)


if __name__ == "__main__":
    main()
