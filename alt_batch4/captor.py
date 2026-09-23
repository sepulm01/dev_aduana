#!/usr/bin/env python3
"""Captor alt_batch4: lectura de codigo GARANTIZADA por pasada, con
cascada heuristica de OCR.

Diferencias con alt_batch3:
- PAQUETE por run: se guarda el crop de TODO cls3 (incluidos los
  sub-umbral), no solo los >= umbral.
- COORDINADOR OCR (pool de workers) en lugar del worker FIFO:
    F1: PaddleOCR liviano (puerto 5004, ~75ms) para crops horizontales
    F2: VL (/spotting para crops verticales, /ocr para el resto)
    F3: DeepSeek API, solo para los top-N crops por area del run
- PARADA TEMPRANA por consenso (>=N lecturas consistentes): se dropean
  los crops pendientes de ese run y se pasa al siguiente camion.
- REGLA TEXTO: si K crops seguidos devuelven texto sin forma ISO 6346,
  el run se marca "texto" y se descarta el lote.
- REGLA VERTICAL: crops con aspecto alto (h > ratio*w) saltan Paddle
  y van directo al VL.

Uso: python captor.py --camara 1 --rtsp rtsp://... \
       [--raw raw_b4/cam1.jsonl] [--video test.mkv] [--sin-ocr] [--gui]
"""
import argparse
import base64
import json
import os
import queue
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter

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

DS_URL = "https://api.deepseek.com/chat/completions"
DS_PROMPT = ("Lee el codigo ISO 6346 del contenedor (4 letras + 7 digitos) "
             "en la imagen. Responde SOLO el codigo de 11 caracteres o NO.")


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


PESOS_TIER = {"strict": 2.0, "repaired": 1.0, "raw": 0.5}


def _consenso_lecturas(lecturas, pesos, n_min, max_dist=2):
    """Devuelve (ganador, total) si un grupo fuzzy (lev<=2) suma >= n_min
    en votos ponderados; el ganador es el de mayor peso del grupo."""
    grupos = []
    for c in lecturas:
        for g in grupos:
            if ocr_codes._levenshtein(c, g[0]) <= max_dist:
                g.append(c)
                break
        else:
            grupos.append([c])
    for g in grupos:
        total = sum(pesos[c] for c in g)
        if total >= n_min:
            return max(g, key=lambda c: pesos[c]), total
    return None, 0


def procesar_camara(args, gate, gate_pose, ctx):
    camara = args.camara
    signo_dir = 1 if camara == 1 else -1
    escala = (W * H) / (960 * 540)
    min_crop = args.min_area_crop * escala
    min_ocr = args.min_area_ocr * escala
    paso_espaciado = max(1, int(round(args.fps)))
    retro = paso_espaciado // 2

    os.makedirs(os.path.dirname(args.raw) or ".", exist_ok=True)
    os.makedirs(f"crops_b4/cam{camara}", exist_ok=True)
    os.makedirs("fotos_b4", exist_ok=True)

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
    runs = ctx["runs"]

    def append_rec(rec):
        with ctx["lock"]:
            with open(args.raw, "a") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def next_seq():
        with ctx["lock"]:
            ctx["seq"] += 1
            return ctx["seq"]

    # ---------------- coordinador OCR ----------------
    def _paddle(crop_path):
        with open(crop_path, "rb") as fh:
            r = requests.post(args.server_paddle + "/ocr",
                              files={"file": fh}, timeout=180)
        r.raise_for_status()
        textos = r.json().get("texts", []) or []
        cods = ocr_codes.extraer_codigos(ocr_codes.limpiar("\n".join(textos)))
        return [(c, t) for c, t, _ in cods], " | ".join(textos)[:120]

    def _vl(crop_path, vertical):
        endpoint = "/spotting" if vertical else "/ocr"
        with open(crop_path, "rb") as fh:
            r = requests.post(args.server + endpoint,
                              files={"file": fh}, timeout=180)
        r.raise_for_status()
        texto = r.json().get("text", "")
        cods = ocr_codes.extraer_codigos(ocr_codes.limpiar(texto))
        return [(c, t) for c, t, _ in cods], texto[:120]

    def _deepseek(crop_path):
        img = base64.b64encode(open(crop_path, "rb").read()).decode()
        payload = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{img}"}},
                {"type": "text", "text": DS_PROMPT}]}],
            "max_tokens": 30, "temperature": 0}
        r = requests.post(DS_URL, json=payload,
                          headers={"Authorization":
                                   f"Bearer {ctx['ds_key']}"},
                          timeout=180)
        r.raise_for_status()
        texto = r.json()["choices"][0]["message"]["content"].strip()
        cods = ocr_codes.extraer_codigos(ocr_codes.limpiar(texto))
        return [(c, t) for c, t, _ in cods], texto[:120]

    def probar(crop_path, vertical):
        cods, texto, motor = [], "", ""
        if not vertical:
            try:
                cods, texto = _paddle(crop_path)
                motor = "paddle"
            except requests.RequestException as e:
                print(f"[ocr] paddle ERROR {e}", flush=True)
        if not cods:
            try:
                cods, texto = _vl(crop_path, vertical)
                motor = "vl"
            except requests.RequestException as e:
                print(f"[ocr] vl ERROR {e}", flush=True)
        if not cods and args.max_ds > 0:
            r = runs.get(run_actual(crop_path))
            with ctx["lock"]:
                permite_ds = (r is not None and not r["detenido"] and
                              r["intentos_ds"] < args.max_ds and
                              area_top3(r, area_por_crop.get(crop_path, 0)))
            if permite_ds:
                try:
                    cods, texto = _deepseek(crop_path)
                    motor = "deepseek"
                except requests.RequestException as e:
                    print(f"[ocr] deepseek ERROR {e}", flush=True)
        return cods, texto, motor

    def area_top3(r, area):
        tops = sorted(r["areas"], reverse=True)[:args.max_ds]
        return area >= (tops[-1] if tops else 0)

    def run_actual(crop_path):
        with ctx["lock"]:
            for run, r in runs.items():
                if crop_path in r["crops"]:
                    return run
        return None

    area_por_crop = {}

    def worker_coord(idx):
        while True:
            item = ctx["q"].get()
            try:
                if item is None:
                    return
                run, crop_path, area, vertical, seq_det = item
                r = runs.get(run)
                if r is None or r["detenido"]:
                    continue
                if area in r["pendientes"]:
                    r["pendientes"].remove(area)
                cods, texto, motor = probar(crop_path, vertical)
                with ctx["lock"]:
                    r = runs.get(run)
                    if r is None or r["detenido"]:
                        continue
                    r["intentos"] += 1
                    if motor == "deepseek":
                        r["intentos_ds"] += 1
                    for c, t in cods:
                        r["lecturas"][c] += 1
                        r["pesos"][c] = r["pesos"].get(
                            c, 0) + PESOS_TIER.get(t, 1.0)
                    if cods:
                        r["max_area_leida"] = max(r["max_area_leida"],
                                                  area)
                    append_rec({"tipo": "ocr", "cam": camara,
                                "seq": seq_det, "crop": crop_path,
                                "motor": motor, "run": run,
                                "codigos": [[c, t] for c, t in cods],
                                "texto": texto})
                    if cods:
                        ganador, total = _consenso_lecturas(
                            r["lecturas"], r["pesos"], args.consenso)
                        if ganador:
                            r["consenso"] = ganador
                            grandes = [a for a in r["pendientes"]
                                       if a >= 0.5 * r["max_area_leida"]]
                            if not grandes:
                                r["detenido"] = True
                                r["veredicto"] = "consenso"
                                append_rec({"tipo": "veredicto",
                                            "cam": camara, "run": run,
                                            "clase": "consenso",
                                            "codigo": ganador,
                                            "ts": round(time.time(), 2)})
                                print(f"[run r{run}] CONSENSO {ganador} "
                                      f"({total} lecturas, {r['intentos']} "
                                      "intentos) -> parando OCR del run",
                                      flush=True)
                            else:
                                print(f"[run r{run}] consenso {ganador} "
                                      f"diferido ({len(grandes)} crops "
                                      "grandes pendientes)", flush=True)
                    elif not r["lecturas"] and len(r["textos"]) >= \
                            args.k_texto:
                        r["detenido"] = True
                        r["veredicto"] = "texto"
                        append_rec({"tipo": "veredicto", "cam": camara,
                                    "run": run, "clase": "texto",
                                    "ts": round(time.time(), 2)})
                        print(f"[run r{run}] TEXTO (sin codigo en "
                              f"{args.k_texto} crops) -> descartar lote",
                              flush=True)
                    if not r["detenido"] and r["consenso"]:
                        grandes = [a for a in r["pendientes"]
                                   if a >= 0.5 * r["max_area_leida"]]
                        if not grandes:
                            r["detenido"] = True
                            r["veredicto"] = "consenso"
                            append_rec({"tipo": "veredicto",
                                        "cam": camara, "run": run,
                                        "clase": "consenso",
                                        "codigo": r["consenso"],
                                        "ts": round(time.time(), 2)})
                    if not r["detenido"] and r["intentos"] >= \
                            args.max_intentos:
                        r["detenido"] = True
                        r["veredicto"] = "agotado"
                        print(f"[run r{run}] AGOTADO "
                              f"({r['intentos']} intentos)", flush=True)
            finally:
                ctx["q"].task_done()

    for i in range(args.pool):
        threading.Thread(target=worker_coord, args=(i,), daemon=True).start()

    def encolar(run, lista):
        for area, crop_path, vertical, seq_det in lista:
            if crop_path in area_por_crop and area_por_crop[crop_path] \
                    >= area:
                continue
            area_por_crop[crop_path] = area
            ctx["q"].put((run, crop_path, area, vertical, seq_det))

    # ---------------- captura ----------------
    def flush_pendientes():
        nonlocal pendientes, n_ocr_run
        lote = sorted(pendientes, key=lambda x: -(x[0].get("area", 0)))
        for rec, crop_arr in lote:
            seq = next_seq()
            rec["seq"] = seq
            path = os.path.join(
                "crops_b4", f"cam{camara}",
                f"s{seq:08d}_f{rec['f']:06d}_a{int(rec['area']):06d}.jpg")
            if crop_arr is not None:
                _guardar_jpg(crop_arr, path, 90)
                rec["crop"] = path
            append_rec(rec)
            if not args.sin_ocr and rec["area"] >= min_ocr:
                r = runs.setdefault(rec["run"], {
                    "detenido": False, "veredicto": None,
                    "lecturas": Counter(), "pesos": {},
                    "textos": [],
                    "intentos": 0, "intentos_ds": 0,
                    "areas": [], "crops": set(),
                    "pendientes": [], "consenso": None,
                    "max_area_leida": 0})
                r["areas"].append(rec["area"])
                r["crops"].add(path)
                ctx["q"].put((rec["run"], path, rec["area"],
                              rec.get("vertical", False), seq))
                r["pendientes"].append(rec["area"])
                n_ocr_run += 1
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
                    "fotos_b4",
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
                    "fotos_b4",
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
                        # los recortes de sello van a fotos_b3 para que el
                        # clasificador (labels_web) los siga consumiendo
                        sc = os.path.join(
                            "fotos_b3",
                            f"cam{camara}_b4_{seq:08d}_f{fpos:06d}_sello.jpg")
                        os.makedirs("fotos_b3", exist_ok=True)
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
        best_code = []
        tops_sellos = []
        n_dets_run = 0
        n_sellos_run = 0
        n_ocr_run = 0

    try:
        while True:
            res = ventana.obtener(pos)
            if res is None:
                if not ventana.eof and ventana.dq:
                    pos = ventana.dq[0][0]
                    continue
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
                    valida = None
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
                            vertical = (y2 - y1) > args.ratio_alto * \
                                (x2 - x1)
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
                                   "vertical": vertical,
                                   "crop": None}
                            if area >= min_crop:
                                pendientes.append((rec, crop))
                            else:
                                pendientes.append((rec, None))
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
                                valida = med * signo_dir > 0.5
                                if valida:
                                    print(f"[dir] f{pos} "
                                          f"{'izq->der' if camara == 1 else 'der->izq'}"
                                          " VALIDA", flush=True)
                                    flush_pendientes()
                                else:
                                    print(f"[dir] f{pos} "
                                          f"{'der->izq' if camara == 1 else 'izq->der'}"
                                          " DESCARTADA", flush=True)
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


def _ds_key(args):
    if args.deepseek_key:
        return args.deepseek_key
    env = os.environ.get("DEEPSEEK_API_KEY")
    if env:
        return env
    try:
        with open(os.path.expanduser(
                "~/.local/share/opencode/auth.json")) as fh:
            return json.load(fh)["deepseek"]["key"]
    except (OSError, ValueError, KeyError):
        return ""


def main():
    global W, H
    sys.stdout.reconfigure(line_buffering=True)
    p = argparse.ArgumentParser()
    p.add_argument("--camara", type=int, required=True, choices=[1, 2])
    p.add_argument("--rtsp", default="")
    p.add_argument("--video", default="")
    p.add_argument("--raw", default="")
    p.add_argument("--tamano", default=f"{W}x{H}")
    p.add_argument("--cada-denso", type=int, default=4)
    p.add_argument("--conf", type=float, default=0.40)
    p.add_argument("--fps", type=float, default=20.0)
    p.add_argument("--server", default=SERVER)
    p.add_argument("--server-paddle", default="http://localhost:5004")
    p.add_argument("--min-area-crop", type=float, default=300.0,
                   help="area minima (eq 540p) para guardar el crop")
    p.add_argument("--min-area-ocr", type=float, default=500.0,
                   help="area minima (eq 540p) para intentar OCR")
    p.add_argument("--ratio-alto", type=float, default=1.2,
                   help="aspecto h/w sobre el cual el crop se considera "
                        "vertical (va directo al VL)")
    p.add_argument("--consenso", type=int, default=3,
                   help="lecturas consistentes para detener el OCR del run")
    p.add_argument("--k-texto", type=int, default=2,
                   help="crops con texto sin codigo para descartar el lote")
    p.add_argument("--max-intentos", type=int, default=12)
    p.add_argument("--max-ds", type=int, default=3,
                   help="crops top por area que pueden ir a DeepSeek")
    p.add_argument("--pool", type=int, default=3)
    p.add_argument("--deepseek-key", default="")
    p.add_argument("--sin-ocr", action="store_true")
    p.add_argument("--gui", action="store_true")
    args = p.parse_args()
    W, H = map(int, args.tamano.split("x"))
    if not args.raw:
        args.raw = os.path.join("raw_b4", f"cam{args.camara}.jsonl")
    if not args.rtsp and not args.video:
        p.error("--rtsp o --video requerido")

    gate = YoloGate(model_path=None, conf_thres=args.conf, imgsz=640)
    gate.warmup()
    gate_pose = PoseGate()
    gate_pose.warmup()
    ctx = {"q": queue.Queue(), "runs": {}, "seq": 0,
           "lock": threading.RLock(), "ds_key": _ds_key(args)}
    try:
        procesar_camara(args, gate, gate_pose, ctx)
    except KeyboardInterrupt:
        pass
    print("[coord] esperando drenar la cola OCR...", flush=True)
    ctx["q"].join()
    for _ in range(args.pool):
        ctx["q"].put(None)


if __name__ == "__main__":
    main()
