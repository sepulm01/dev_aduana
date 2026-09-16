#!/usr/bin/env python3
"""Re-compara GTs cacheados (rangos_m{G} post-procesados) contra los rangos
guardados por rango_extraer, SIN re-inferir ni re-correr nada."""
import argparse
import csv
import glob
import json
import os
import sqlite3


def solapa(a, b):
    return a[0] <= b[1] and b[0] <= a[1]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="test_report")
    p.add_argument("--gap", type=int, default=45)
    p.add_argument("--csv", default=None)
    args = p.parse_args()

    csv_path = args.csv or os.path.join(args.out, "comparativa_merged.csv")
    with open(csv_path, "w", newline="") as fcsv:
        wr = csv.writer(fcsv)
        wr.writerow([
            "video", "n_gt", "n_det", "matched", "missed", "extras",
            "dini_med", "dfin_med", "dini_mean", "dfin_mean", "run_s",
            "gt_durs_s", "det_durs_s",
        ])
        for path in sorted(glob.glob(os.path.join(args.out, "gt_*.json"))):
            gt = json.load(open(path))
            nombre = os.path.basename(path)[3:-5]
            key = f"rangos_m{args.gap}"
            if key not in gt:
                continue
            gt_r = [tuple(r) for r in gt[key]]
            run_dir = os.path.join(args.out, f"run_{nombre}")
            db = os.path.join(run_dir, "procesamiento.db")
            if not os.path.exists(db):
                continue
            conn = sqlite3.connect(db)
            det = [tuple(r) for r in conn.execute(
                "SELECT inicio, fin FROM rangos ORDER BY idx")]
            run_s = conn.execute(
                "SELECT elapsed_s FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            conn.close()
            run_s = run_s[0] if run_s else None

            usados, matched, missed = set(), 0, 0
            dinis, dfins = [], []
            for g in gt_r:
                cands = [i for i, d in enumerate(det)
                         if solapa(g, d) and i not in usados]
                if not cands:
                    missed += 1
                    continue
                i = cands[0]
                usados.add(i)
                matched += 1
                dinis.append(det[i][0] - g[0])
                dfins.append(det[i][1] - g[1])
            extras = len(det) - len(usados)
            dinis.sort()
            dfins.sort()

            def med(x):
                return x[len(x) // 2] if x else None

            wr.writerow([
                nombre, len(gt_r), len(det), matched, missed, extras,
                med(dinis), med(dfins),
                round(sum(dinis) / len(dinis), 1) if dinis else None,
                round(sum(dfins) / len(dfins), 1) if dfins else None,
                run_s,
                [round((b - a) / 20, 1) for a, b in gt_r],
                [round((b - a) / 20, 1) for a, b in det],
            ])
    print(f"-> {csv_path}")


if __name__ == "__main__":
    main()
