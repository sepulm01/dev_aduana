#!/usr/bin/env python3
"""Pipeline RTSP en tiempo real (una camara por proceso).

- ffmpeg hwaccel cuda decodifica el RTSP a 4K y alimenta una ventana
  deslizante (con reconexion automatica y wall-clock por frame).
- YOLO espaciado (1/s) -> denso (cada --cada-denso) con rewind, regla de
  trayectoria (camion nuevo en la rafaga), direccion (cam1: izq->der
  valido; cam2: sin filtro).
- Crops cls3 del MISMO frame 4K -> OCR (hilo aparte).
- Sellos: top-K frames con mas dets cls0/1 -> pose kpt3 al cierre.
- Al cerrar cada rafaga emite un evento JSONL con todo lo necesario para
  la conciliacion (lecturas, trayectoria, sellos, fotos, crops).

Uso: .venv/bin/python stream.py --camara 1 --rtsp rtsp://... \
       [--eventos eventos/cam1.jsonl] [--umbral-area 3000] [--gui]
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
                   _sello_en_kpt3, _veredicto_frames)
from yolo_gate import YoloGate

W, H = 3840, 2160
VENTANA_4K = 40
PAD_AREA = 1500.0


def leer_rtsp(url, ventana, W, H, detener, proc_holder):
    """Lector con reconexion: ffmpeg RTSP a 4K, frames con wall-clock."""
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


def worker_ocr(ctx):
    while True:
        item = ctx["q"].get()
        try:
            if item is None:
                if ctx["cerrar"]:
                    return
                continue
            bid, pos, area, png = item
            t1 = time.time()
            try:
                r = requests.post(SERVER + "/ocr", files={"file": png},
                                  timeout=180)
                r.raise_for_status()
                texto = r.json().get("text", "")
            except requests.RequestException as e:
                print(f"[ocr b{bid}] f{pos} ERROR {e}", flush=True)
            else:
                ms = (time.time() - t1) * 1000
                lineas = ocr_codes.limpiar(texto)
                cods = ocr_codes.extraer_codigos(lineas)
                ctx["res"].setdefault(bid, {})[pos] = {
                    "texto": texto, "ms": round(ms),
                    "codigos": [(c, t) for c, t, _ in cods]}
                if cods:
                    for code, tier, _ in cods:
                        ctx["votos"].setdefault(bid, []).append(code)
                    print(f"[ocr b{bid}] f{pos} a={area:.0f} {ms:.0f}ms -> "
                          + ", ".join(f"{c}({t})" for c, t, _ in cods),
                          flush=True)
                else:
                    print(f"[ocr b{bid}] f{pos} a={area:.0f} "
                          f"sin codigo <- {texto!r}", flush=True)
        finally:
            ctx["q"].task_done()
            he = ctx["hechos"].get(bid)
            if he:
                he[0] += 1
                if he[0] >= he[1]:
                    he[2].set()


def emitir_evento(lote, ctx, args):
    """Espera las lecturas OCR del burst y emite la linea JSONL."""
    bid = lote["bid"]
    he = ctx["hechos"].get(bid)
    if he and he[1]:
        he[2].wait(timeout=180)
    ctx["hechos"].pop(bid, None)
    res_ocr = ctx["res"].pop(bid, {})
    votos = ctx["votos"].pop(bid, [])

    cls3 = []
    for det in lote["cls3"]:
        f = det["frame"]
        o = res_ocr.get(f)
        cod = None
        if o and o["codigos"]:
            cod = o["codigos"][0][0]
        cls3.append([f, det["cx"], det["cy"], det["area"],
                     det["conf"], cod])
    lecturas = []
    for det in lote["cls3"]:
        o = res_ocr.get(det["frame"])
        if o:
            for c, _ in o["codigos"]:
                lecturas.append([det["frame"], c])

    fotos = []
    foto_crops = []
    if lote["tops"]:
        os.makedirs("fotos_rtsp", exist_ok=True)
        for i, (n, idx, fcopy, dets_s) in enumerate(lote["tops"]):
            kpt = None
            for mf in lote["sellos_analysis"].get("mejores_frames", []):
                if mf["frame"] == idx:
                    kpt = mf.get("kpt3")
                    break
            img = fcopy.copy()
            for d in dets_s:
                x1, y1, x2, y2 = d["bbox"]
                color = (0, 255, 255) if d["cls"] == 0 else (255, 0, 255)
                cv2.rectangle(img, (int(x1), int(y1)), (int(x2), int(y2)),
                              color, 3)
            if kpt:
                kx, ky = int(kpt["x"] * W), int(kpt["y"] * H)
                cv2.circle(img, (kx, ky), 40, (0, 255, 0), 6)
            path = os.path.join("fotos_rtsp",
                                f"cam{args.camara}_b{bid}_{i}.jpg")
            ok, buf = cv2.imencode(".jpg", img,
                                   [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                with open(path, "wb") as fh:
                    fh.write(buf.tobytes())
                fotos.append(path)
            cpath = None
            if kpt:
                cx, cy = kpt["x"] * W, kpt["y"] * H
                half = 0.07 * W
                for d in dets_s:
                    x1, y1, x2, y2 = d["bbox"]
                    bw, bh = x2 - x1, y2 - y1
                    bx, by = (x1 + x2) / 2, (y1 + y2) / 2
                    if abs(bx - cx) <= 2.5 * bw and abs(by - cy) <= 2.5 * bh:
                        half = max(half, 2.2 * bw, 2.2 * bh)
                x1, y1 = max(0, int(cx - half)), max(0, int(cy - half))
                x2, y2 = min(W, int(cx + half)), min(H, int(cy + half))
                crop = fcopy[y1:y2, x1:x2]
                ok, buf = cv2.imencode(".jpg", crop,
                                       [cv2.IMWRITE_JPEG_QUALITY, 90])
                if ok:
                    cpath = os.path.join(
                        "fotos_rtsp",
                        f"cam{args.camara}_b{bid}_{i}_sello.jpg")
                    with open(cpath, "wb") as fh:
                        fh.write(buf.tobytes())
            foto_crops.append(cpath)

    evento = {
        "tipo": "pasada", "camara": args.camara, "bid": bid,
        "ts_inicio": round(lote["ts_inicio"], 2),
        "ts_fin": round(lote["ts_fin"], 2),
        "frame_inicio": lote["frame_inicio"],
        "frame_fin": lote["frame_fin"],
        "valida": lote["valida"],
        "n_sellos": lote["n_sellos"],
        "n_cls3": len(lote["cls3"]),
        "cls3": cls3, "lecturas": lecturas,
        "sellos": lote["sellos_analysis"],
        "sellos_tops": [{"frame": mf["frame"], "n_sellos": mf["n_sellos"],
                         "cls": mf.get("cls"), "conf": mf.get("conf")}
                        for mf in
                        lote["sellos_analysis"].get("mejores_frames", [])],
        "fotos": fotos,
        "foto_crops": foto_crops,
        "crops": lote["crops"],
        "codigos_votos": {c: votos.count(c) for c in set(votos)},
    }
    with open(args.eventos, "a") as fh:
        fh.write(json.dumps(evento, ensure_ascii=False) + "\n")
    s = lote["sellos_analysis"]["veredicto"]
    print(f"[evento b{bid}] f{lote['frame_inicio']}-{lote['frame_fin']} "
          f"ts={lote['ts_inicio']:.0f}-{lote['ts_fin']:.0f} sello={s} "
          f"lecturas={len(votos)} fotos={len(fotos)}", flush=True)


def lanzar_cierre(lote, ctx, args):
    threading.Thread(target=emitir_evento, args=(lote, ctx, args),
                     daemon=True).start()


def procesar_camara(args, gate, gate_pose, ctx):
    camara = args.camara
    filtrar_dir = camara == 1
    umbral_eq = args.umbral_area or (3000 if camara == 1 else 2000)
    umbral_ef = umbral_eq * (W * H) / (960 * 540)
    umbral_chico = PAD_AREA * (W * H) / (960 * 540)
    paso_espaciado = max(1, int(round(args.fps)))
    retro = paso_espaciado // 2

    os.makedirs(os.path.dirname(args.eventos), exist_ok=True)
    os.makedirs(f"crops_rtsp/cam{camara}", exist_ok=True)

    detener = {"on": False}
    proc_holder = {"proc": None}
    ventana = Ventana(VENTANA_4K)
    lector = threading.Thread(target=leer_rtsp,
                              args=(args.rtsp, ventana, W, H, detener,
                                    proc_holder),
                              daemon=True)
    lector.start()

    pos = 0
    modo = "espaciado"
    sin_interes = 0
    dir_dx = []
    valida = None
    pendientes_ocr = []
    traj_prev = None
    bid = 0
    dets_burst = []
    tops_burst = []
    burst_frame0 = 0
    burst_ts0 = 0.0
    ts_frame = {}
    n_inf = 0

    def cerrar_burst():
        nonlocal bid, dets_burst, tops_burst, valida, pendientes_ocr
        sellos_res = {"veredicto": "sin datos", "cls": None,
                      "conf": None, "duda": True}
        if valida and tops_burst:
            frames_res = []
            for n, idx, fcopy, dets_s in tops_burst:
                r = _sello_en_kpt3(fcopy, dets_s, gate_pose, W, H)
                frames_res.append({"frame": idx, "n_sellos": n, **r})
            sellos_res = _veredicto_frames(frames_res)
            sellos_res = dict(sellos_res, mejores_frames=frames_res)
        lote = {"bid": bid, "cls3": list(dets_burst),
                "tops": list(tops_burst),
                "sellos_analysis": sellos_res,
                "ts_inicio": burst_ts0, "ts_fin": ts_frame.get(pos, 0.0),
                "frame_inicio": burst_frame0, "frame_fin": pos - 1,
                "valida": valida, "n_sellos": n_sellos_burst,
                "crops": list(crops_burst)}
        lanzar_cierre(lote, ctx, args)
        dets_burst = []
        tops_burst = []
        crops_burst.clear()
        pendientes_ocr = []

    n_sellos_burst = 0
    crops_burst = []

    try:
        while True:
            res = ventana.obtener(pos)
            if res is None:
                break
            frame, ts = res
            ts_frame[pos] = ts
            hacer = (modo == "denso" and pos % args.cada_denso == 0) or \
                    (modo == "espaciado" and pos % paso_espaciado == 0)
            interes = cls3 = sellos = []
            if hacer:
                n_inf += 1
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
                    pendientes_ocr = []
                    traj_prev = None
                    bid += 1
                    dets_burst = []
                    tops_burst = []
                    n_sellos_burst = 0
                    crops_burst = []
                    burst_frame0 = pos
                    burst_ts0 = ts_frame.get(pos, ts)
                    ctx["hechos"][bid] = [0, 0, threading.Event()]
                    ventana.liberar_hasta(max(0, pos - retro))
                    continue
                pos += 1
            else:
                descartada_ahora = False
                if hacer:
                    if interes:
                        sin_interes = 0
                        mejor = max(interes, key=lambda d: d[4])
                        if cls3:
                            m3 = max(cls3, key=lambda d: d[4])
                            cx3 = (float(m3[0]) + float(m3[2])) / 2
                            a3 = (float(m3[2]) - float(m3[0])) * \
                                 (float(m3[3]) - float(m3[1]))
                            if traj_prev is not None:
                                dx = cx3 - traj_prev[0]
                                entrada = ((camara == 1 and cx3 < 0.2 * W
                                            and traj_prev[0] > 0.8 * W) or
                                           (camara == 2 and cx3 > 0.8 * W
                                            and traj_prev[0] < 0.2 * W))
                                if entrada and abs(dx) >= 0.25 * W \
                                        and a3 >= 5 * traj_prev[1] \
                                        and traj_prev[1] < umbral_chico:
                                    print(f"[traj] f{pos} nuevo camion en "
                                          "la rafaga", flush=True)
                                    cerrar_burst()
                                    sin_interes = 0
                                    dir_dx = []
                                    valida = None if filtrar_dir else True
                                    traj_prev = None
                                    bid += 1
                                    dets_burst = []
                                    tops_burst = []
                                    n_sellos_burst = 0
                                    crops_burst = []
                                    burst_frame0 = pos
                                    burst_ts0 = ts
                                    ctx["hechos"][bid] = \
                                        [0, 0, threading.Event()]
                            traj_prev = (cx3, a3)
                        for d in cls3:
                            x1, y1, x2, y2 = map(float, d[:4])
                            conf = float(d[4])
                            area = (x2 - x1) * (y2 - y1)
                            det = {"frame": pos, "conf": conf,
                                   "cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2,
                                   "area": area, "bbox": [x1, y1, x2, y2]}
                            dets_burst.append(det)
                            if area >= umbral_ef:
                                px, py = int((x2 - x1) * PAD_X), \
                                    int((y2 - y1) * PAD_Y)
                                cx1 = max(0, int(x1) - px)
                                cx2 = min(W, int(x2) + px)
                                cy1 = max(0, int(y1) - py)
                                cy2 = min(H, int(y2) + py)
                                crop = frame[cy1:cy2, cx1:cx2]
                                _, buf = cv2.imencode(".png", crop)
                                cp = os.path.join(
                                    "crops_rtsp", f"cam{camara}",
                                    f"b{bid}_f{pos:06d}_a{int(area):06d}.png")
                                cv2.imwrite(cp, crop)
                                crops_burst.append(cp)
                                if valida is True:
                                    ctx["q"].put((bid, pos, area,
                                                  buf.tobytes()))
                                    ctx["hechos"][bid][1] += 1
                                elif valida is None:
                                    pendientes_ocr.append(
                                        (bid, pos, area, buf.tobytes()))
                        if sellos:
                            n_sellos_burst += len(sellos)
                            dets_s = [{"cls": int(d[5]), "conf": float(d[4]),
                                       "bbox": [float(d[0]), float(d[1]),
                                                float(d[2]), float(d[3])]}
                                      for d in sellos]
                            tops_burst.append((len(sellos), pos,
                                               frame.copy(), dets_s))
                            tops_burst.sort(key=lambda x: -x[0])
                            tops_burst = tops_burst[:args.top_sellos]
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
                                    for pn in pendientes_ocr:
                                        ctx["q"].put(pn)
                                        ctx["hechos"][bid][1] += 1
                                    pendientes_ocr = []
                                else:
                                    print(f"[dir] f{pos} der->izq "
                                          "DESCARTADA", flush=True)
                                    pendientes_ocr = []
                                    modo = "espaciado"
                                    descartada_ahora = True
                    else:
                        sin_interes += args.cada_denso
                        if sin_interes >= SILENCIO_DENSO:
                            print(f"[yolo] f{pos} silencio -> espaciado",
                                  flush=True)
                            modo = "espaciado"
                            cerrar_burst()
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
    sys.stdout.reconfigure(line_buffering=True)
    p = argparse.ArgumentParser()
    p.add_argument("--camara", type=int, required=True, choices=[1, 2])
    p.add_argument("--rtsp", required=True)
    p.add_argument("--eventos", default="")
    p.add_argument("--umbral-area", type=float, default=None)
    p.add_argument("--cada-denso", type=int, default=4)
    p.add_argument("--top-sellos", type=int, default=2)
    p.add_argument("--conf", type=float, default=0.40)
    p.add_argument("--fps", type=float, default=20.0)
    p.add_argument("--server", default=SERVER)
    p.add_argument("--gui", action="store_true")
    args = p.parse_args()
    if not args.eventos:
        args.eventos = os.path.join("eventos", f"cam{args.camara}.jsonl")

    gate = YoloGate(model_path=None, conf_thres=args.conf, imgsz=640)
    gate.warmup()
    gate_pose = PoseGate()
    gate_pose.warmup()
    ctx = {"q": queue.Queue(maxsize=64), "res": {}, "votos": {},
           "hechos": {}, "cerrar": False}
    ocr_t = threading.Thread(target=worker_ocr, args=(ctx,), daemon=True)
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
