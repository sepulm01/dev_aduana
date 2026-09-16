#!/usr/bin/env python3
"""Consulta detecciones por clase y umbral de confianza desde procesamiento.db.

Uso:
  .venv/bin/python buscar_por_clase.py --db frames_yolo/procesamiento.db \
      --clase 3 --conf 0.5
  .venv/bin/python buscar_por_clase.py --clase 3 --conf 0.5 \
      --output mejores_codigos        # copia los JPGs de esos frames
"""
import argparse
import os
import shutil
import sqlite3

CLASES = ["con_sello", "sin_sello", "cont data", "container cod", "truck"]


def main():
    p = argparse.ArgumentParser(description="Filtra detecciones por clase y confianza")
    p.add_argument("--db", default="procesamiento.db", help="ruta a procesamiento.db")
    p.add_argument("--clase", type=int, required=True, help="id de clase 0-4")
    p.add_argument("--conf", type=float, default=0.5, help="confianza minima")
    p.add_argument("--output", default=None,
                   help="carpeta donde copiar los frames que pasan el filtro")
    p.add_argument("--max", type=int, default=0, help="limitar a N frames (top conf)")
    p.add_argument("--frames-dir", default=None,
                   help="directorio con processed_*.jpg (default: el del --db)")
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()
    cur.execute(
        "SELECT frame, MAX(conf) FROM dets WHERE cls=? AND conf>=? "
        "GROUP BY frame ORDER BY 2 DESC",
        (args.clase, args.conf))
    rows = cur.fetchall()
    conn.close()

    if args.max:
        rows = rows[:args.max]

    nombre_clase = CLASES[args.clase] if 0 <= args.clase < len(CLASES) else str(args.clase)
    print(f"clase {args.clase} ({nombre_clase}) con conf >= {args.conf}: {len(rows)} frames")
    for frame, conf in rows:
        print(f"  frame {frame:>6d}  conf={conf:.3f}  processed_{frame:06d}.jpg")

    if args.output:
        frames_dir = args.frames_dir or os.path.dirname(os.path.abspath(args.db))
        os.makedirs(args.output, exist_ok=True)
        copiados = 0
        for frame, _ in rows:
            src = os.path.join(frames_dir, f"processed_{frame:06d}.jpg")
            if os.path.exists(src):
                shutil.copy(src, args.output)
                copiados += 1
        print(f"copiados {copiados}/{len(rows)} frames a {args.output}")


if __name__ == "__main__":
    main()
