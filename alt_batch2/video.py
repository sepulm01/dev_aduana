#!/usr/bin/env python3
"""Pipeline online con colas:

- Decode GPU (ffmpeg cuda) en un hilo: deja frames reducidos en una
  ventana deslizante (proceso en paralelo, con backpressure).
- YOLO consume a su ritmo: modo espaciado (1 inferencia/s) sobre frames
  reducidos. Al ver interes (cls3 codigo o cls0/1 sellos) retrocede medio
  segundo en la ventana y pasa a modo denso (inferencia cada
  --cada-denso frames).
- Las detecciones cls3 se miden por area del bbox; si superan
  --umbral-area se encolan al OCR (hilo aparte, no bloquea YOLO).
- El OCR valida los codigos (ISO 6346) y al final se hace consenso.
- Sellos: se conservan los --top-sellos frames con mas detecciones cls0/1
  de cada pasada; al cerrar la pasada se corre el pose sobre ellos y se
  decide si hay sello en el keypoint 3.
"""
import argparse
import collections
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
import estado
from pose_gate import PoseGate
from yolo_gate import YoloGate

VIDEO = "/var/www/dev_aduana/alt_batch2/videos/cam1_20260901_144704.mkv"
W, H = 960, 540
SERVER = "http://localhost:5003"
VENTANA = 40
SILENCIO_DENSO = 30
PAD_X, PAD_Y = 0.15, 0.30
CONF_SELLO = 0.6
TOL_ASOC = 1.0


def _sello_en_kpt3(frame, dets_s, gate_pose, W, H):
    """Pose sobre el frame y busqueda del bbox de sello mas cercano al
    kpt3 (indice 2), con rect expandido por TOL_ASOC (logica de
    cerrar_camion)._foto_y_sello). Devuelve dict kpt3/cls/conf."""
    res = {"kpt3": None, "cls": None, "conf": None}
    dets_pose = gate_pose.detect(frame)
    if not len(dets_pose):
        return res
    mejor = max(dets_pose, key=lambda d: float(gate_pose.kpts_de(d)[2][2]))
    kpts = gate_pose.kpts_de(mejor)
    nx, ny = float(kpts[2][0]) / W, float(kpts[2][1]) / H
    res["kpt3"] = {"x": round(nx, 4), "y": round(ny, 4),
                   "conf": round(float(kpts[2][2]), 3)}
    best = None
    for s in dets_s:
        x1, y1, x2, y2 = s["bbox"]
        bw, bh = x2 - x1, y2 - y1
        ex1, ey1 = x1 - TOL_ASOC * bw, y1 - TOL_ASOC * bh
        ex2, ey2 = x2 + TOL_ASOC * bw, y2 + TOL_ASOC * bh
        if not (ex1 <= nx * W <= ex2 and ey1 <= ny * H <= ey2):
            continue
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        dist = ((nx * W - cx) ** 2 + (ny * H - cy) ** 2) ** 0.5
        if best is None or dist < best[0] - 1e-9 or \
                (abs(dist - best[0]) < 1e-9 and s["conf"] > best[1]):
            best = (dist, s["conf"], s["cls"])
    if best:
        res["cls"] = best[2]
        res["conf"] = round(best[1], 3)
    return res


def _veredicto_frames(frames_res):
    """Veredicto de una camara: unanimidad entre los frames top con
    conf >= CONF_SELLO; cualquier discrepancia o ausencia -> DUDA."""
    v = {"veredicto": "sin datos", "cls": None, "conf": None, "duda": True}
    if not frames_res:
        return v
    ok = [r for r in frames_res
          if r["cls"] is not None and r["conf"] is not None
          and r["conf"] >= CONF_SELLO]
    if len(ok) == len(frames_res):
        clases = {r["cls"] for r in ok}
        if len(clases) == 1:
            cls = clases.pop()
            v = {"veredicto": "CON SELLO" if cls == 0 else "SIN SELLO",
                 "cls": cls, "conf": min(r["conf"] for r in ok),
                 "duda": False}
            return v
    v["veredicto"] = "DUDA"
    return v


class Ventana:
    def __init__(self, maxlen):
        self.dq = collections.deque()
        self.maxlen = maxlen
        self.cond = threading.Condition()
        self.eof = False

    def poner(self, idx, frame, ts=None):
        with self.cond:
            while len(self.dq) >= self.maxlen and not self.eof:
                self.cond.wait()
            self.dq.append((idx, ts, frame))
            self.cond.notify_all()

    def terminar(self):
        with self.cond:
            self.eof = True
            self.cond.notify_all()

    def liberar_hasta(self, idx):
        with self.cond:
            while self.dq and self.dq[0][0] < idx:
                self.dq.popleft()
            self.cond.notify_all()

    def obtener(self, idx):
        with self.cond:
            while True:
                if self.dq and self.dq[-1][0] >= idx:
                    break
                if self.eof:
                    if not self.dq or self.dq[-1][0] < idx:
                        return None
                    break
                self.cond.wait()
            for n, ts, fr in self.dq:
                if n == idx:
                    return fr, ts
            return None


def leer_frames(proc, ventana, frame_bytes, W, H):
    idx = 0
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            ventana.poner(idx, np.frombuffer(buf, np.uint8).reshape(H, W, 3))
            idx += 1
    finally:
        ventana.terminar()


def worker_ocr(ctx):
    while True:
        item = ctx["q"].get()
        try:
            if item is None:
                if ctx["cerrar"]:
                    return
                continue
            idx, area, png = item
            t1 = time.time()
            try:
                r = requests.post(SERVER + "/ocr", files={"file": png},
                                  timeout=180)
                r.raise_for_status()
                texto = r.json().get("text", "")
            except requests.RequestException as e:
                print(f"[ocr] f{idx} ERROR {e}")
                continue
            ms = (time.time() - t1) * 1000
            lineas = ocr_codes.limpiar(texto)
            cods = ocr_codes.extraer_codigos(lineas)
            ctx["res"][idx] = {"texto": texto, "ms": round(ms),
                               "codigos": [(c, t) for c, t, _ in cods]}
            if cods:
                for code, tier, _ in cods:
                    ctx["votos"].append(code)
                print(f"[ocr] f{idx} a={area:.0f} {ms:.0f}ms -> "
                      + ", ".join(f"{c}({t})" for c, t, _ in cods))
            else:
                print(f"[ocr] f{idx} a={area:.0f} sin codigo <- {texto!r}")
        finally:
            ctx["q"].task_done()


def escanar_pendientes(permitidos, vistos):
    """mkv completos (tamano estable en 2 chequeos, 3s) y sin procesar."""
    pend = []
    ahora = time.time()
    for f in sorted(os.listdir("videos")):
        if not f.endswith(".mkv"):
            continue
        base = f[:-4]
        if permitidos and base not in permitidos:
            continue
        path = os.path.join("videos", f)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        prev = vistos.get(f)
        if prev is None or prev[0] != size:
            vistos[f] = (size, ahora)
        elif ahora - prev[1] >= 3.0:
            pend.append(base)
            vistos.pop(f, None)
    return pend


def extraer_frames_4k(video, frames):
    """Extrae frames del mkv original a 4K (decode GPU; fallback CPU)."""
    frames = sorted(set(frames))
    if not frames:
        return {}
    sel = "+".join(f"eq(n,{f})" for f in frames)
    W4, H4 = 3840, 2160
    for hw in (True, False):
        vf = f"select='{sel}'" + (",hwdownload,format=nv12" if hw else "")
        cmd = ["ffmpeg", "-v", "error", "-y"]
        if hw:
            cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        cmd += ["-i", video, "-vf", vf, "-vsync", "0",
                "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, timeout=900)
        n = len(proc.stdout) // (W4 * H4 * 3)
        if n == len(frames):
            arr = np.frombuffer(proc.stdout, np.uint8).reshape(n, H4, W4, 3)
            return {f: arr[i] for i, f in enumerate(frames)}
    return {}


def decodificar_cmd(video, W, H, hw):
    if hw:
        return ["ffmpeg", "-v", "error",
                "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                "-i", video,
                "-vf", f"scale_cuda={W}:{H},hwdownload,format=nv12",
                "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    return ["ffmpeg", "-v", "error",
            "-i", video,
            "-vf", f"scale={W}:{H}",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]


def sonda_hw(video):
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
         "-i", video, "-frames:v", "5", "-f", "null", "-"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    return r.returncode == 0


def procesar_video(args, gate, gate_pose, ctx):
    base = os.path.splitext(os.path.basename(args.video))[0]
    dir_crops = os.path.join("crops_area", base)
    camara = 1 if base.startswith("cam1_") else 2
    filtrar_dir = camara == 1
    W, H = 960, 540
    umbral_eq = args.umbral_area if args.umbral_area else \
        (3000 if camara == 1 else 2000)
    umbral_ef = umbral_eq * (W * H) / (960 * 540)

    cap = cv2.VideoCapture(args.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 20.0
    cap.release()
    paso_espaciado = max(1, int(round(fps)))
    retro = max(1, int(round(fps / 2)))

    if args.decode == "cpu":
        usar_hw = False
    elif args.decode == "gpu":
        usar_hw = True
    else:
        usar_hw = sonda_hw(args.video)
        if not usar_hw:
            print(f"{base}: GPU sin espacio para hwaccel -> decode por CPU")
    cmd = decodificar_cmd(args.video, W, H, usar_hw)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    ventana = Ventana(VENTANA)
    lector = threading.Thread(
        target=leer_frames, args=(proc, ventana, W * H * 3, W, H),
        daemon=True)
    lector.start()

    ocr_q = ctx["q"]
    ocr_res = ctx["res"] = {}
    lista_votos = ctx["votos"] = []

    os.makedirs(dir_crops, exist_ok=True)
    dets_todas = []

    pedidos_4k = []

    t0 = time.time()
    pos = 0
    modo = "espaciado"
    sin_interes = 0
    dir_dx = []
    valida = None
    pendientes_ocr = []
    idx_inicio_pasada = 0
    top_sellos = []
    sellos_frame = {}
    analisis_sellos = []
    burst_inicio = 0
    n_inf = 0
    n_cls3 = 0
    n_sellos = 0
    n_ocr = 0
    n_frames = 0
    n_descartadas = 0
    pose_ms = 0.0
    traj_prev = None
    umbral_chico = 1500 * (W * H) / (960 * 540)

    def cerrar_burst():
        nonlocal top_sellos, sellos_frame, analisis_sellos, pose_ms
        if valida and top_sellos:
            frames_res = []
            for n, idx, fcopy, dets_s in top_sellos:
                t1 = time.time()
                r = _sello_en_kpt3(fcopy, dets_s, gate_pose, W, H)
                pose_ms += (time.time() - t1) * 1000
                frames_res.append({"frame": idx, "n_sellos": n, **r})
            v = _veredicto_frames(frames_res)
            analisis_sellos.append({"inicio": burst_inicio,
                                    "mejores_frames": frames_res, **v})
            print(f"[sello] f{burst_inicio}: {v['veredicto']} "
                  + " | ".join(f"f{f['frame']} n={f['n_sellos']} "
                               f"cls={f['cls']} c={f['conf']}"
                               for f in frames_res))
        top_sellos = []
        sellos_frame = {}
    interrumpido = False
    try:
        while True:
            frame, _ = ventana.obtener(pos)
            if frame is None:
                if modo == "denso":
                    cerrar_burst()
                break
            n_frames += 1
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
                    print(f"[yolo] f{pos} interes -> modo denso "
                          f"(rewind {retro})")
                    modo = "denso"
                    pos = max(0, pos - retro)
                    sin_interes = 0
                    dir_dx = []
                    valida = None if filtrar_dir else True
                    pendientes_ocr = []
                    idx_inicio_pasada = len(dets_todas)
                    burst_inicio = pos
                    traj_prev = None
                    ventana.liberar_hasta(max(0, pos - retro))
                    continue
                pos += 1
            else:
                descartada_ahora = False
                if hacer:
                    if interes:
                        sin_interes = 0
                        frame = frame.copy()
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
                                    print(f"[traj] f{pos} nuevo camion "
                                          "en la rafaga")
                                    cerrar_burst()
                                    sin_interes = 0
                                    dir_dx = []
                                    valida = None if filtrar_dir else True
                                    pendientes_ocr = []
                                    idx_inicio_pasada = len(dets_todas)
                                    burst_inicio = pos
                            traj_prev = (cx3, a3)
                        for d in cls3:
                            n_cls3 += 1
                            x1, y1, x2, y2 = map(float, d[:4])
                            conf = float(d[4])
                            area = (x2 - x1) * (y2 - y1)
                            enviado = area >= umbral_ef
                            det = {"frame": pos, "cls": 3,
                                   "conf": round(conf, 3),
                                   "bbox": [round(v, 1) for v in (x1, y1, x2, y2)],
                                   "area": round(area),
                                   "cx": round((x1 + x2) / 2, 1),
                                   "cy": round((y1 + y2) / 2, 1),
                                   "enviado_ocr": enviado,
                                   "valida": valida}
                            dets_todas.append(det)
                            color = (0, 255, 0) if enviado else (0, 0, 255)
                            cv2.rectangle(frame, (int(x1), int(y1)),
                                          (int(x2), int(y2)), color, 2)
                            if enviado:
                                item = {"pos": pos,
                                        "bbox": [x1, y1, x2, y2],
                                        "area": area}
                                if valida is True:
                                    pedidos_4k.append(item)
                                    n_ocr += 1
                                elif valida is None:
                                    pendientes_ocr.append(item)
                        for d in sellos:
                            n_sellos += 1
                            x1, y1, x2, y2 = map(float, d[:4])
                            conf = float(d[4])
                            cls = int(d[5])
                            det = {"frame": pos, "cls": cls,
                                   "conf": round(conf, 3),
                                   "bbox": [round(v, 1) for v in (x1, y1, x2, y2)],
                                   "area": round((x2 - x1) * (y2 - y1)),
                                   "cx": round((x1 + x2) / 2, 1),
                                   "cy": round((y1 + y2) / 2, 1),
                                   "enviado_ocr": False,
                                   "valida": valida}
                            dets_todas.append(det)
                            color = (0, 255, 255) if cls == 0 else (255, 0, 255)
                            cv2.rectangle(frame, (int(x1), int(y1)),
                                          (int(x2), int(y2)), color, 1)
                            s = {"cls": cls, "conf": conf,
                                 "bbox": [x1, y1, x2, y2]}
                            sellos_frame.setdefault(pos, []).append(s)
                        if sellos:
                            top_sellos.append((len(sellos), pos, frame,
                                               sellos_frame[pos]))
                            top_sellos.sort(key=lambda x: -x[0])
                            top_sellos = top_sellos[:args.top_sellos]
                        dir_dx.append((float(mejor[0]) + float(mejor[2])) / 2)
                        if valida is None and len(dir_dx) >= 4:
                            med = statistics.median(
                                [dir_dx[i + 1] - dir_dx[i]
                                 for i in range(len(dir_dx) - 1)])
                            if abs(med) > 0.5 or len(dir_dx) >= 6:
                                valida = med > 0.5
                                for dd in dets_todas[idx_inicio_pasada:]:
                                    dd["valida"] = valida
                                if valida:
                                    print(f"[dir] f{pos} izq->der VALIDA")
                                    pedidos_4k.extend(pendientes_ocr)
                                    n_ocr += len(pendientes_ocr)
                                    pendientes_ocr = []
                                else:
                                    n_descartadas += 1
                                    pendientes_ocr = []
                                    top_sellos = []
                                    sellos_frame = {}
                                    print(f"[dir] f{pos} der->izq "
                                          "DESCARTADA (calle del frente)")
                                    modo = "espaciado"
                                    descartada_ahora = True
                    else:
                        sin_interes += args.cada_denso
                        if sin_interes >= SILENCIO_DENSO:
                            print(f"[yolo] f{pos} silencio -> modo espaciado")
                            modo = "espaciado"
                            cerrar_burst()
                pos += paso_espaciado * 2 if descartada_ahora else 1

            ventana.liberar_hasta(max(0, pos - retro))
            cv2.imshow(f"cam{camara} {base}", frame)
            cv2.moveWindow(f"cam{camara} {base}",
                           0 if camara == 1 else 1000, 0)
            if cv2.waitKey(1) == ord("q"):
                break
    except KeyboardInterrupt:
        print("interrumpido")
        interrumpido = True
    finally:
        proc.terminate()
        ventana.terminar()
        cv2.destroyAllWindows()
        lector.join(timeout=10)
    W4, H4 = 3840, 2160
    with estado.GpuLock():
        for i in range(0, len(pedidos_4k), 8):
            chunk = pedidos_4k[i:i + 8]
            imgs = extraer_frames_4k(args.video, [p["pos"] for p in chunk])
            for p in chunk:
                img = imgs.get(p["pos"])
                if img is None:
                    continue
                x1, y1, x2, y2 = [v * 4 for v in p["bbox"]]
                px, py = int((x2 - x1) * PAD_X), int((y2 - y1) * PAD_Y)
                cx1 = max(0, int(x1) - px)
                cx2 = min(W4, int(x2) + px)
                cy1 = max(0, int(y1) - py)
                cy2 = min(H4, int(y2) + py)
                crop = img[cy1:cy2, cx1:cx2]
                _, buf = cv2.imencode(".png", crop)
                cv2.imwrite(os.path.join(
                    dir_crops, f"f{p['pos']:06d}_a{int(p['area']):06d}.png"),
                    crop)
                ocr_q.put((p["pos"], p["area"], buf.tobytes()))
    ctx["q"].join()

    elapsed = time.time() - t0
    pasadas = []
    actual = None
    for det in dets_todas:
        if actual is None or det["frame"] - actual["fin_frame"] > paso_espaciado:
            actual = {"id": len(pasadas) + 1, "inicio_frame": det["frame"],
                      "fin_frame": det["frame"], "direccion": None,
                      "detecciones": []}
            pasadas.append(actual)
        actual["fin_frame"] = det["frame"]
        det_guard = dict(det)
        res_ocr = ocr_res.get(det["frame"])
        if res_ocr:
            det_guard["ocr"] = res_ocr
        actual["detecciones"].append(det_guard)
    for p in pasadas:
        cxs = [d["cx"] for d in p["detecciones"] if d["cls"] == 3]
        if not cxs:
            cxs = [d["cx"] for d in p["detecciones"] if d["cls"] in (0, 1)]
        if len(cxs) >= 3:
            dxs = [cxs[i + 1] - cxs[i] for i in range(len(cxs) - 1)]
            med = statistics.median(dxs)
            p["direccion"] = "izq->der" if med > 0.5 else \
                ("der->izq" if med < -0.5 else "estatico")
        p["valida"] = (p["direccion"] == "izq->der") if filtrar_dir else True
        ana = None
        for a in analisis_sellos:
            if p["inicio_frame"] - 15 <= a["inicio"] <= p["fin_frame"] + 15:
                ana = a
                break
        if ana:
            p["sellos"] = {"mejores_frames": ana["mejores_frames"],
                           "veredicto": ana["veredicto"],
                           "cls": ana["cls"], "conf": ana["conf"],
                           "duda": ana["duda"]}
        else:
            p["sellos"] = {"veredicto": "sin datos", "cls": None,
                           "conf": None, "duda": True}
        print(f"[pasada {p['id']}] f{p['inicio_frame']}-{p['fin_frame']} "
              f"n={len(p['detecciones'])} dir={p['direccion']} "
              f"{'VALIDA' if p['valida'] else 'descartada'} "
              f"sello={p['sellos']['veredicto']}")

    res = ocr_codes.consenso(lista_votos, min_votos=2, max_distancia=2)
    from collections import Counter
    votos = Counter(lista_votos)
    print(f"frames={n_frames} inferencias={n_inf} cls3={n_cls3} "
          f"sellos={n_sellos} a_ocr={n_ocr} descartadas={n_descartadas} "
          f"lecturas={len(lista_votos)} elapsed={elapsed:.1f}s "
          f"pose={gate_pose.calls} ({pose_ms:.0f}ms, "
          f"carga={ctx['pose_carga']:.1f}s)")
    if votos:
        for code, v in votos.most_common():
            print(f"  {code}: {v} lecturas")
    confirmados = [c for c, v in votos.items() if v >= 2]
    print("CONFIRMADOS: " + (", ".join(confirmados) if confirmados else "ninguno"))
    print(f"CONSENSO (mejor): {res if res else 'SIN CODIGO CONFIRMADO'}")

    salida = {
        "video": args.video,
        "camara": camara,
        "filtrar_dir": filtrar_dir,
        "fps": fps,
        "frame_size": [W, H],
        "params": {"umbral_eq": umbral_eq,
                   "umbral_efectivo": round(umbral_ef),
                   "cada_denso": args.cada_denso,
                   "conf": args.conf,
                   "silencio_denso": SILENCIO_DENSO},
        "pasadas": pasadas,
        "resumen": {"frames": n_frames, "inferencias": n_inf,
                    "cls3": n_cls3, "sellos": n_sellos,
                    "a_ocr": n_ocr, "descartadas": n_descartadas,
                    "lecturas": len(lista_votos),
                    "pose_calls": gate_pose.calls,
                    "pose_ms": round(pose_ms),
                    "pose_carga_s": round(ctx["pose_carga"], 1),
                    "confirmados": confirmados, "consenso": res,
                    "elapsed": round(elapsed, 1)},
    }
    with open(f"detecciones_{base}.json", "w") as fh:
        json.dump(salida, fh, indent=1)
    print(f"JSON: detecciones_{base}.json ({len(dets_todas)} detecciones, "
          f"{len(pasadas)} pasadas)")
    if interrumpido:
        raise KeyboardInterrupt
    return salida


def main():
    sys.stdout.reconfigure(line_buffering=True)
    p = argparse.ArgumentParser()
    p.add_argument("--video", default=VIDEO)
    p.add_argument("--umbral-area", type=float, default=None,
                   help="area minima equivalente 540p para enviar a OCR "
                        "(default: 3000 cam1, 2000 cam2; 0 = todo)")
    p.add_argument("--decode", default="auto",
                   choices=["auto", "gpu", "cpu"],
                   help="decoder de la fuente: auto (sonda GPU), gpu o cpu")
    p.add_argument("--cada-denso", type=int, default=4,
                   help="frames entre inferencias en modo denso (2-4)")
    p.add_argument("--conf", type=float, default=0.40)
    p.add_argument("--server", default=SERVER)
    p.add_argument("--top-sellos", type=int, default=2,
                   help="frames con mas sellos a analizar por pasada")
    p.add_argument("--continuo", action="store_true",
                   help="modo continuo: carga modelos una vez y procesa "
                        "los videos que llegan a videos/")
    p.add_argument("--videos", default="",
                   help="en modo continuo: solo estos videos (sin .mkv, "
                        "separados por coma); vacio = todos")
    args = p.parse_args()

    gate = YoloGate(model_path=None, conf_thres=args.conf, imgsz=640)
    gate.warmup()
    t_pose0 = time.time()
    gate_pose = PoseGate()
    gate_pose.warmup()
    ctx = {"q": queue.Queue(maxsize=64), "res": {}, "votos": [],
           "cerrar": False,
           "pose_carga": round(time.time() - t_pose0, 1)}
    print(f"modelos cargados (pose {ctx['pose_carga']:.1f}s)")
    ocr_t = threading.Thread(target=worker_ocr, args=(ctx,), daemon=True)
    ocr_t.start()

    if not args.continuo:
        try:
            procesar_video(args, gate, gate_pose, ctx)
        except KeyboardInterrupt:
            pass
        ctx["cerrar"] = True
        ctx["q"].put(None)
        ocr_t.join(timeout=30)
        return

    permitidos = {s.strip() for s in args.videos.split(",") if s.strip()}
    vistos = {}
    procesados = set()
    t_inicio = time.time()
    print("continuo: mirando videos/" +
          (f" (solo: {args.videos})" if permitidos else ""))
    try:
        while True:
            for nombre in escanar_pendientes(permitidos, vistos):
                if nombre in procesados:
                    continue
                print(f"== procesando {nombre} ==")
                args.video = os.path.join("videos", nombre + ".mkv")
                procesar_video(args, gate, gate_pose, ctx)
                procesados.add(nombre)
            time.sleep(3.0)
    except KeyboardInterrupt:
        print("shutdown...")
    ctx["cerrar"] = True
    ctx["q"].put(None)
    ocr_t.join(timeout=30)
    cv2.destroyAllWindows()
    print(f"continuo: {len(procesados)} videos en "
          f"{time.time() - t_inicio:.0f}s")


if __name__ == "__main__":
    main()
