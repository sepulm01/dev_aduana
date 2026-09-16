import cv2
import os
import argparse
import time
import statistics
import sqlite3

try:
    from yolo_gate import YoloGate, MODEL_PATH as YOLO_MODEL_DEFAULT
except ImportError:
    YoloGate = None
    YOLO_MODEL_DEFAULT = None

# Estrategia de skip adaptativo con prediccion de descenso:
#   QUIETO        px < T/3 confirmado  -> salta K-1 frames (grab, sin decode)
#   VIGILIA       T/3 <= px < T        -> MOG2 en cada frame, no guarda
#   ACTIVO        px >= T (medido)     -> guarda si area pasa; predice cuantos
#                                         frames faltan para bajar del umbral
#   ACTIVO_SALTO  prediccion > K       -> salta MOG2 durante K frames y guarda
#                                         TODOS los frames a ciegas (decode +
#                                         imwrite); re-mide al cabo de K
SKIP_K = 4              # frames entre mediciones (quietud y rafaga)
GRAY_DIV = 3            # px < T/GRAY_DIV = quieto profundo
QUIET_STREAK = 2        # muestras consecutivas en quietud para entrar a QUIETO
DESCENT_INIT = 65.0     # pendiente de descenso inicial (px/frame, mediana global)


def detectar_actividad(video_path, output_dir="frames_con_actividad",
                         umbral_area=500, min_pixeles_fg=1000, visual=False,
                         max_frames=0, skip=True, yolo=True, yolo_conf=0.40,
                         yolo_model=None, solo_clase=-1):
    """
    Recorre un video, aplica MOG2 y guarda los frames con movimiento.

    Optimizacion (skip=True):
      * QUIETO: salta SKIP_K-1 frames con cap.grab() (sin decodificar).
      * ACTIVO_SALTO: dentro de una rafaga, el MOG2 se corre solo cada
        SKIP_K frames; los frames intermedios se guardan a ciegas (la
        actividad de un vehiculo persiste muchos frames: se predice
        cuantos faltan para bajar del umbral con la pendiente de descenso
        observada en la propia rafaga). Si el vehiculo termina antes de
        lo predicho, se guardan a lo mas SKIP_K-1 frames de mas.
      * VIGILIA y la cola (prediccion <= K): cada frame con MOG2.

    Con visual=True se muestra overlay con valores medidos, estado,
    modo de medicion (mog2/ciego) y prediccion.
    """
    os.makedirs(output_dir, exist_ok=True)
    # Reset de salidas de corridas anteriores sobre la misma carpeta
    for f in os.listdir(output_dir):
        if f.startswith("processed_") and f.endswith(".jpg"):
            try:
                os.remove(os.path.join(output_dir, f))
            except OSError:
                pass
    for f in ("metricas.csv", "procesamiento.db",
              "procesamiento.db-wal", "procesamiento.db-shm"):
        try:
            os.remove(os.path.join(output_dir, f))
        except FileNotFoundError:
            pass

    log_file = open(os.path.join(output_dir, "metricas.csv"), "w")
    log_file.write("frame,pixeles_movimiento,area_max,umbral_pixeles,umbral_area,"
                   "actividad,estado,modo,frames_restantes,yolo_clases,skipped_acum\n")

    # Almacen principal: SQLite (frames + detecciones YOLO por frame)
    db_path = os.path.join(output_dir, "procesamiento.db")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS frames (
        frame INTEGER PRIMARY KEY,
        px INTEGER, area INTEGER,
        umb_px INTEGER, umb_area INTEGER,
        actividad INTEGER,
        estado TEXT, modo TEXT,
        frames_restantes REAL, skipped_acum INTEGER,
        t_s REAL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS dets (
        frame INTEGER, cls INTEGER, conf REAL,
        x1 REAL, y1 REAL, x2 REAL, y2 REAL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT, elapsed_s REAL,
        video TEXT, umb_px INTEGER, umb_area INTEGER,
        yolo INTEGER, yolo_conf REAL, skip INTEGER,
        total_frames INTEGER, skipped INTEGER,
        guardados INTEGER, yolo_llamadas INTEGER,
        fps_promedio REAL)""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_dets_cls_conf ON dets (cls, conf)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_dets_frame ON dets (frame)")
    conn.commit()

    gate = None
    if yolo and YoloGate is not None:
        gate = YoloGate(model_path=yolo_model or YOLO_MODEL_DEFAULT,
                        conf_thres=yolo_conf)
        gate.warmup()
        print(f"YOLO gate activo (conf>={yolo_conf})")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise IOError(f"No se pudo abrir el video: {video_path}")

    fgbg = cv2.createBackgroundSubtractorMOG2(
        history=500,
        varThreshold=16,
        detectShadows=True
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

    win_name = "Pre-proceso MOG2"
    if visual:
        cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(win_name, 1280, 720)

    frame_idx = 0
    guardados = 0
    t_inicio = time.time()
    pausa = False
    estado = "VIGILIA"          # arranca denso (MOG2 calentandose)
    quiet_streak = 0
    skipped_acum = 0
    limite_quieto = min_pixeles_fg // GRAY_DIV

    # Tracking de la rafaga activa para la prediccion
    descensos = []              # pendientes px/frame observadas en el descenso
    prev_px = None              # px de la medicion anterior (dentro de rafaga)
    prev_px_frame = None        # frame de esa medicion
    frames_restantes = 0.0
    saltar_restantes = 0
    rafaga_con_clase = False    # la ultima medicion YOLO confirmo clase

    while True:
        if pausa:
            key = cv2.waitKey(30) & 0xFF
            if key == ord('p'):
                pausa = False
            elif key == ord('q'):
                break
            continue

        # ---- Rama ACTIVO_SALTO: guardar a ciegas solo si hay clase confirmada ----
        if estado == "ACTIVO_SALTO" and skip:
            if rafaga_con_clase:
                ret, frame = cap.read()
                if not ret:
                    break
                nombre_salida = os.path.join(output_dir, f"processed_{frame_idx:06d}.jpg")
                cv2.imwrite(nombre_salida, frame)
                guardados += 1
                log_file.write(f"{frame_idx},-1,-1,{min_pixeles_fg},{umbral_area},1,"
                               f"{estado},ciego,{frames_restantes:.0f},-1,{skipped_acum}\n")
                cur.execute(
                    "INSERT INTO frames VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (frame_idx, -1, -1, min_pixeles_fg, umbral_area, 1,
                     estado, "ciego", frames_restantes, skipped_acum,
                     time.time() - t_inicio))
                if visual:
                    display = _visual(frame, frame_idx, guardados, t_inicio, estado,
                                      "ciego", -1, -1, min_pixeles_fg, umbral_area,
                                      frames_restantes, skipped_acum, True)
                    cv2.imshow(win_name, display)
                    key = cv2.waitKey(1) & 0xFF
                    if key == ord('q'):
                        break
                    elif key == ord('p'):
                        pausa = True
            else:
                # sin clase confirmada: saltar el frame sin decodificar
                if not cap.grab():
                    break
                skipped_acum += 1

            saltar_restantes -= 1
            if saltar_restantes <= 0:
                estado = "ACTIVO"   # proxima iteracion mide con MOG2

            frame_idx += 1
            if max_frames and frame_idx >= max_frames:
                break
            continue

        # ---- Rama QUIETO: saltar K-1 frames sin decodificar ----
        if estado == "QUIETO" and skip:
            ok = True
            for _ in range(SKIP_K - 1):
                if not cap.grab():
                    ok = False
                    break
            if not ok:
                break
            skipped_acum += SKIP_K - 1
            frame_idx += SKIP_K - 1

        # ---- Medicion con MOG2 ----
        ret, frame = cap.read()
        if not ret:
            break

        frame_small = cv2.resize(frame, None, fx=0.05, fy=0.05,
                                 interpolation=cv2.INTER_AREA)
        fgmask = fgbg.apply(frame_small)
        _, fgmask_clean = cv2.threshold(fgmask, 200, 255, cv2.THRESH_BINARY)
        fgmask_clean = cv2.morphologyEx(fgmask_clean, cv2.MORPH_OPEN, kernel)
        pixeles_movimiento = cv2.countNonZero(fgmask_clean)
        contornos, _ = cv2.findContours(fgmask_clean, cv2.RETR_EXTERNAL,
                                        cv2.CHAIN_APPROX_SIMPLE)
        max_area = max((cv2.contourArea(c) for c in contornos), default=0)

        # ---- Transiciones de estado ----
        if pixeles_movimiento >= min_pixeles_fg:
            quiet_streak = 0
            # actualizar pendientes de descenso de esta rafaga
            if prev_px is not None and pixeles_movimiento < prev_px:
                dist = frame_idx - prev_px_frame
                if dist >= 1:
                    descensos.append((prev_px - pixeles_movimiento) / dist)
            pend = statistics.median(descensos) if descensos else DESCENT_INIT
            frames_restantes = (pixeles_movimiento - min_pixeles_fg) / max(pend, 1.0)
            if frames_restantes > SKIP_K and skip:
                estado = "ACTIVO_SALTO"
                saltar_restantes = SKIP_K
            else:
                estado = "ACTIVO"
            prev_px = pixeles_movimiento
            prev_px_frame = frame_idx
        elif pixeles_movimiento < limite_quieto:
            if estado == "ACTIVO":
                estado = "VIGILIA"
            rafaga_con_clase = False
            quiet_streak += 1
            if estado == "VIGILIA" and quiet_streak >= QUIET_STREAK:
                estado = "QUIETO"
            if estado == "QUIETO":
                descensos, prev_px, prev_px_frame = [], None, None
        else:
            estado = "VIGILIA"
            rafaga_con_clase = False
            quiet_streak = 0
            if prev_px is not None:
                descensos, prev_px, prev_px_frame = [], None, None

        # ---- Decision de guardado (YOLO como gate definitivo si esta activo) ----
        yolo_clases = -1
        dets_actuales = []
        if pixeles_movimiento > min_pixeles_fg:
            if gate is not None:
                tiene, dets = gate.has_class(frame)
                yolo_clases = len(dets)
                dets_actuales = dets
                if solo_clase >= 0:
                    # guardar solo frames que contengan la clase pedida
                    tiene = any(int(d[5]) == solo_clase for d in dets)
                actividad = tiene
                rafaga_con_clase = tiene
            else:
                actividad = max_area > umbral_area
                rafaga_con_clase = True
        else:
            actividad = False

        log_file.write(f"{frame_idx},{pixeles_movimiento},{int(max_area)},"
                       f"{min_pixeles_fg},{umbral_area},{1 if actividad else 0},"
                       f"{estado},mog2,{frames_restantes:.0f},{yolo_clases},{skipped_acum}\n")

        cur.execute(
            "INSERT INTO frames VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (frame_idx, pixeles_movimiento, int(max_area), min_pixeles_fg,
             umbral_area, 1 if actividad else 0, estado, "mog2",
             frames_restantes, skipped_acum, time.time() - t_inicio))
        for d in dets_actuales:
            cur.execute(
                "INSERT INTO dets VALUES (?,?,?,?,?,?,?)",
                (frame_idx, int(d[5]), float(d[4]),
                 float(d[0]), float(d[1]), float(d[2]), float(d[3])))
        conn.commit()

        if actividad:
            nombre_salida = os.path.join(output_dir, f"processed_{frame_idx:06d}.jpg")
            cv2.imwrite(nombre_salida, frame)
            guardados += 1

        if visual:
            display = _visual(frame, frame_idx, guardados, t_inicio, estado,
                              "mog2", pixeles_movimiento, max_area,
                              min_pixeles_fg, umbral_area, frames_restantes,
                              skipped_acum, actividad, yolo_clases)
            cv2.imshow(win_name, display)
            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                break
            elif key == ord('p'):
                pausa = True

        frame_idx += 1
        if max_frames and frame_idx >= max_frames:
            break

    cap.release()
    if visual:
        cv2.destroyAllWindows()
    elapsed = time.time() - t_inicio
    cur.execute(
        "INSERT INTO runs (started_at, elapsed_s, video, umb_px, umb_area, "
        "yolo, yolo_conf, skip, total_frames, skipped, guardados, "
        "yolo_llamadas, fps_promedio) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (time.strftime("%Y-%m-%d %H:%M:%S"), round(elapsed, 1), video_path,
         min_pixeles_fg, umbral_area,
         1 if gate is not None else 0, yolo_conf, 1 if skip else 0,
         frame_idx, skipped_acum, guardados,
         gate.calls if gate is not None else 0,
         round(frame_idx / max(elapsed, 1e-6), 2)))
    conn.commit()
    log_file.close()
    conn.close()
    yolo_info = f", YOLO llamadas: {gate.calls}" if gate is not None else ""
    print(f"Procesados {frame_idx} frames (skipped {skipped_acum}). "
          f"Guardados con actividad: {guardados}{yolo_info}. "
          f"Tiempo: {elapsed:.1f}s")


def _visual(frame, frame_idx, guardados, t_inicio, estado, modo,
            px, area, umb_px, umb_area, frames_restantes, skipped_acum, activo,
            yolo_clases=-1):
    display = frame.copy()
    color = (0, 255, 0) if activo else (0, 0, 255)
    alto = display.shape[0]
    escala = alto / 1080.0
    grosor = max(1, int(2 * escala))
    f1 = max(0.6, 0.9 * escala)
    fps = frame_idx / max(time.time() - t_inicio, 1e-6)

    fila = 40 * escala
    if modo == "ciego":
        lineas = [
            f"frame {frame_idx}  modo=CIEGO (sin MOG2)  estado={estado}",
            f"prediccion: ~{frames_restantes:.0f} frames hasta bajar del umbral",
            f"FPS = {fps:.1f}   guardados = {guardados}   skipped {skipped_acum}",
        ]
    else:
        yolo_txt = "?" if yolo_clases < 0 else str(yolo_clases)
        lineas = [
            f"frame {frame_idx}  px = {px} (umbral {umb_px})  estado={estado}  yolo_dets={yolo_txt}",
            f"area max = {int(area)}  (umbral {umb_area})  pred={frames_restantes:.0f}",
            f"FPS = {fps:.1f}   guardados = {guardados}   skipped {skipped_acum}",
        ]
    for linea in lineas:
        cv2.putText(display, linea, (20, int(fila)), cv2.FONT_HERSHEY_SIMPLEX,
                    f1, color, grosor, cv2.LINE_AA)
        fila += 45 * escala

    if display.shape[1] > 1600:
        factor = 1600.0 / display.shape[1]
        display = cv2.resize(display, None, fx=factor, fy=factor,
                             interpolation=cv2.INTER_AREA)
    return display


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Detecta y guarda frames con actividad usando MOG2")
    parser.add_argument("video", help="Ruta del video de entrada (mkv/mp4)")
    parser.add_argument("--output", default="frames_con_actividad", help="Carpeta de salida")
    parser.add_argument("--area", type=int, default=500, help="Área mínima de contorno para considerar movimiento")
    parser.add_argument("--pixeles", type=int, default=1000, help="Cantidad mínima de píxeles en movimiento")
    parser.add_argument("--visual", action="store_true",
                        help="Muestra ventana con los valores medidos, umbrales y decision")
    parser.add_argument("--max-frames", type=int, default=0,
                        help="Procesar solo hasta este frame (0 = video completo)")
    parser.add_argument("--no-skip", action="store_true",
                        help="Desactiva todos los skips (linea base A/B)")
    parser.add_argument("--no-yolo", action="store_true",
                        help="Desactiva el gate YOLO (guarda solo con MOG2)")
    parser.add_argument("--yolo-conf", type=float, default=0.40,
                        help="Confianza minima de deteccion YOLO")
    parser.add_argument("--yolo-model", default=None,
                        help="Ruta al best.pt (default: computer_vision/models/yolov9_aduana/best.pt)")
    parser.add_argument("--solo-clase", type=int, default=-1,
                        help="Guardar solo frames que contengan esta clase (0-4). -1 = todas")
    args = parser.parse_args()

    detectar_actividad(args.video, args.output, args.area, args.pixeles, args.visual,
                       args.max_frames, skip=not args.no_skip,
                       yolo=not args.no_yolo, yolo_conf=args.yolo_conf,
                       yolo_model=args.yolo_model, solo_clase=args.solo_clase)
