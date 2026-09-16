#!/usr/bin/env python3
"""Post-procesa GTs cacheados: une rafagas separadas por gaps cortos
(flicker del detector) y descarta clusters demasiado pequenos.
Escribe rangos_m{G} dentro del mismo JSON, sin re-inferir."""
import argparse
import glob
import json
import os


def merge(presentes, cada, gap, min_muestras):
    rangos = []
    if not presentes:
        return rangos
    a = b = presentes[0]
    for f in presentes[1:]:
        if f - b <= gap:
            b = f
        else:
            rangos.append([a, b])
            a = b = f
    rangos.append([a, b])
    return [r for r in rangos if r[1] - r[0] >= cada * (min_muestras - 1)]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="test_report")
    p.add_argument("--gap", type=int, default=45, help="frames para unir gaps")
    p.add_argument("--min-muestras", type=int, default=2)
    p.add_argument("--solo", default=None)
    args = p.parse_args()

    for path in sorted(glob.glob(os.path.join(args.out, "gt_*.json"))):
        if args.solo and args.solo not in path:
            continue
        gt = json.load(open(path))
        cada = gt["cada"]
        presentes = sorted(int(k) for k, v in gt["truck_conf"].items() if v >= 0.40)
        rangos = merge(presentes, cada, args.gap, args.min_muestras)
        gt[f"rangos_m{args.gap}"] = rangos
        json.dump(gt, open(path, "w"))
        nombre = os.path.basename(path)[3:-5]
        print(f"{nombre}: {len(gt['rangos'])} -> {len(rangos)}  "
              f"durs={[round((b-a)/20,1) for a,b in rangos]}")


if __name__ == "__main__":
    main()
