#!/usr/bin/env python3
"""Pipeline completo por video, un solo comando:
   1) rango_extraer (deteccion + cosecha cls3)
   2) ocr_rangos (OCR con early exit + codigos)
   3) reporte (PDF con foto 4K + bbox)
El PDF se genera automaticamente al terminar."""
import argparse
import sys

import ocr_rangos
import pose_evento
import rango_extraer
import reporte


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--refinar", action="store_true", default=True)
    p.add_argument("--max-crops", type=int, default=16)
    p.add_argument("--server", default="http://localhost:5003")
    p.add_argument("--proxy-height", type=int, default=540)
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--sin-pose", action="store_true",
                   help="desactivar la etapa pose (default: activa)")
    p.add_argument("--model-pose", default=None,
                   help="checkpoint del pose (default: best_pose_cierre_v3)")
    args = p.parse_args()

    sys.argv = ["rango_extraer.py", "--video", args.video,
                "--output", args.output,
                "--proxy-height", str(args.proxy_height),
                "--imgsz", str(args.imgsz)]
    if args.refinar:
        sys.argv.append("--refinar")
    rango_extraer.main()

    sys.argv = ["ocr_rangos.py", "--run", args.output, "--video", args.video,
                "--server", args.server, "--max-crops", str(args.max_crops)]
    ocr_rangos.main()

    if not args.sin_pose:
        sys.argv = ["pose_evento.py", "--run", args.output,
                    "--video", args.video]
        if args.model_pose:
            sys.argv += ["--model", args.model_pose]
        pose_evento.main()

    sys.argv = ["reporte.py", "--run", args.output, "--video", args.video]
    reporte.main()


if __name__ == "__main__":
    main()
