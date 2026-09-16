#!/usr/bin/env python3
"""Vigilante de llegada/: detecta archivos completos (sin .part y tamano
estable en dos chequeos), los mueve a videos/ y los encola en estado.db."""
import os
import shutil
import time

import estado


def main():
    estado.crear_tablas()
    os.makedirs(estado.LLEGADA, exist_ok=True)
    os.makedirs(estado.VIDEOS, exist_ok=True)
    # reconciliacion: videos ya presentes sin fila en la cola
    conn = estado.conectar()
    conocidos = {r[0] for r in conn.execute("SELECT nombre FROM segmentos")}
    for f in sorted(os.listdir(estado.VIDEOS)):
        if f.endswith(".mkv") and f[:-4] not in conocidos:
            camara, ts = estado.parse_nombre(f[:-4])
            conn.execute(
                "INSERT OR IGNORE INTO segmentos "
                "(nombre, camara, ts, llegada_at) VALUES (?,?,?,?)",
                (f[:-4], camara, ts, time.time()))
    conn.commit()
    conn.close()
    vistos = {}
    print("vigilante: escaneando", estado.LLEGADA)
    while True:
        try:
            for f in sorted(os.listdir(estado.LLEGADA)):
                if not f.endswith(".mkv"):
                    continue
                path = os.path.join(estado.LLEGADA, f)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                prev = vistos.get(f)
                ahora = time.time()
                if prev and prev[0] == size and ahora - prev[1] >= 3.0:
                    destino = os.path.join(estado.VIDEOS, f)
                    shutil.move(path, destino)
                    nombre = f[:-4]
                    camara, ts = estado.parse_nombre(nombre)
                    conn = estado.conectar()
                    conn.execute(
                        "INSERT OR IGNORE INTO segmentos "
                        "(nombre, camara, ts, llegada_at) VALUES (?,?,?,?)",
                        (nombre, camara, ts, ahora))
                    conn.commit()
                    conn.close()
                    vistos.pop(f, None)
                    print(f"[{time.strftime('%H:%M:%S')}] aceptado {f} "
                          f"-> cola (cam{camara})")
                else:
                    vistos[f] = (size, ahora)
            time.sleep(3)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
