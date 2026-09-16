#!/usr/bin/env python3
"""Planificador de alt_batch2: vigila la cola de segmentos y lanza las
etapas (proxy -> detectar -> ocr -> agrupar) como subprocesos, con N
trabajadores y reintentos. Ademas ejecuta cerrar_camion cuando hay pasadas
cerradas pendientes de consolidar."""
import os
import subprocess
import sys
import time

import estado

ETAPA_SCRIPT = {
    "proxy": "etapa_proxy.py",
    "detectar": "etapa_detectar.py",
    "ocr": "ocr_rangos.py",
    "agrupar": "etapa_agrupar.py",
}
OCR_EXTRA = {"--server": "http://localhost:5003"}
MAX_INTENTOS = 3
TRABAJADORES = 3


def main():
    p = __import__("argparse").ArgumentParser()
    p.add_argument("--trabajadores", type=int, default=TRABAJADORES)
    args = p.parse_args()
    estado.crear_tablas()
    # limpieza de arranque: trabajos marcados en curso por corridas
    # anteriores se liberan (los subprocesos huerfanos son idempotentes)
    conn0 = estado.conectar()
    conn0.execute("UPDATE segmentos SET en_proceso=0")
    conn0.commit()
    conn0.close()
    activos = {}
    cerrar_activo = None
    print("scheduler: iniciado")
    while True:
        try:
            conn = estado.conectar()
            # recoger trabajadores terminados
            terminados = []
            for nombre, proc in list(activos.items()):
                if proc.poll() is not None:
                    terminados.append((nombre, proc))
            for nombre, proc in terminados:
                seg = conn.execute(
                    "SELECT estado, intentos FROM segmentos WHERE nombre=?",
                    (nombre,)).fetchone()
                if seg is None:
                    del activos[nombre]
                    continue
                estado_prev, intentos = seg
                if proc.returncode == 0:
                    nuevo_estado = estado.ETAPAS[estado_prev][1]
                    conn.execute(
                        "UPDATE segmentos SET estado=?, en_proceso=0, "
                        "error=NULL, fin_at=? WHERE nombre=?",
                        (nuevo_estado, time.time(), nombre))
                    print(f"[{time.strftime('%H:%M:%S')}] {nombre}: "
                          f"{estado_prev} ok -> {nuevo_estado}")
                else:
                    intentos += 1
                    if intentos >= MAX_INTENTOS:
                        conn.execute(
                            "UPDATE segmentos SET estado='error', "
                            "en_proceso=0, intentos=?, error=? WHERE "
                            "nombre=?", (intentos, f"exit {proc.returncode}",
                                         nombre))
                        print(f"[{time.strftime('%H:%M:%S')}] {nombre}: "
                              f"ERROR definitivo tras {intentos} intentos")
                    else:
                        conn.execute(
                            "UPDATE segmentos SET en_proceso=0, intentos=? "
                            "WHERE nombre=?", (intentos, nombre))
                        print(f"[{time.strftime('%H:%M:%S')}] {nombre}: "
                              f"{estado_prev} fallo (intento {intentos})")
                conn.commit()
                del activos[nombre]

            # auto-curacion: segmentos marcados en curso cuyo subproceso ya
            # no esta en el dict (corrida reiniciada o excepcion pasada)
            activos_nombres = set(activos)
            huerfanos = conn.execute(
                "SELECT nombre FROM segmentos WHERE en_proceso=1").fetchall()
            for (nombre,) in huerfanos:
                if nombre not in activos_nombres:
                    conn.execute(
                        "UPDATE segmentos SET en_proceso=0 WHERE nombre=?",
                        (nombre,))
            conn.commit()

            # cierre de camiones (un solo proceso a la vez)
            if cerrar_activo is not None and cerrar_activo.poll() is not None:
                cerrar_activo = None
            if cerrar_activo is None:
                pend = conn.execute(
                    "SELECT 1 FROM pasadas WHERE estado='cerrada' AND "
                    "consolidada=0 LIMIT 1").fetchone()
                if pend:
                    cerrar_activo = subprocess.Popen(
                        [sys.executable, "cerrar_camion.py"],
                        cwd=estado.BASE)
                    print(f"[{time.strftime('%H:%M:%S')}] cerrar_camion "
                          f"lanzado")

            # despachar nuevas etapas
            libres = args.trabajadores - len(activos)
            if libres > 0:
                # agrupar se serializa (una pasada abierta por camara no
                # tolera desorden de segmentos) y todo se despacha en
                # orden cronologico dentro de cada etapa
                filas = conn.execute(
                    "SELECT nombre, estado FROM segmentos WHERE estado IN "
                    "('nuevo','detectar','ocr','agrupar') AND en_proceso=0 "
                    "ORDER BY CASE estado WHEN 'nuevo' THEN 0 WHEN "
                    "'detectar' THEN 1 WHEN 'ocr' THEN 2 ELSE 3 END, "
                    "llegada_at LIMIT ?", (libres * 2,)).fetchall()
                filas.sort(key=lambda r: estado.parse_nombre(r[0])[1])
                agrupar_activo = False
                for nombre, est in filas:
                    if len(activos) >= args.trabajadores:
                        break
                    etapa = estado.ETAPAS[est][0]
                    if etapa == "agrupar":
                        # agrupar exige orden cronologico estricto por
                        # camara: no despachar si queda un video anterior
                        # de la misma camara sin agrupar
                        if agrupar_activo:
                            continue
                        cam, ts = estado.parse_nombre(nombre)
                        anterior = conn.execute(
                            "SELECT 1 FROM segmentos o WHERE o.camara=? "
                            "AND o.ts<? AND o.estado IN "
                            "('nuevo','detectar','ocr','agrupar') LIMIT 1",
                            (cam, ts)).fetchone()
                        if anterior:
                            continue
                    script = ETAPA_SCRIPT[etapa]
                    cmd = [sys.executable, script]
                    if etapa == "ocr":
                        cmd += ["--run",
                                os.path.join(estado.PROCESADOS, nombre),
                                "--video",
                                os.path.join(estado.VIDEOS,
                                             nombre + ".mkv"),
                                "--server", "http://localhost:5003"]
                    else:
                        cmd += ["--video", nombre]
                    proc = subprocess.Popen(cmd, cwd=estado.BASE,
                                            stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL)
                    activos[nombre] = proc
                    conn.execute(
                        "UPDATE segmentos SET en_proceso=1 WHERE nombre=?",
                        (nombre,))
                    if etapa == "agrupar":
                        agrupar_activo = True
                    print(f"[{time.strftime('%H:%M:%S')}] {nombre}: "
                          f"lanzando {est}")
                conn.commit()
            conn.close()
            time.sleep(3)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"scheduler: error recuperable: {e}", flush=True)
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(3)


if __name__ == "__main__":
    main()
