#!/usr/bin/env python3
"""Analiza detecciones.json para calibrar --umbral-area:
imprime la tabla area-bin vs exito OCR y la cobertura GT por umbral.

Uso: .venv/bin/python calibrar_umbral.py [detecciones.json]
El GT se pasa con --gt "MNBU3300406,MSDU4752691,..." (fuzzy <=2)."""
import argparse
import json

import ocr_codes


def main():
    p = argparse.ArgumentParser()
    p.add_argument("json", nargs="?", default="detecciones.json")
    p.add_argument("--gt", default="",
                   help="codigos GT separados por coma (fuzzy <=2)")
    args = p.parse_args()
    datos = json.load(open(args.json))
    GT = {c.strip().upper() for c in args.gt.split(",") if c.strip()}

    def cerca(c):
        return not GT or any(ocr_codes._levenshtein(c, g) <= 2 for g in GT)

    dets = [d for p in datos["pasadas"] for d in p["detecciones"]]
    print(f"detecciones={len(dets)} con_ocr={sum('ocr' in d for d in dets)}")

    bins = [(0, 3000), (3000, 6000), (6000, 9000), (9000, 12000),
            (12000, 15000), (15000, 18000), (18000, 25000)]
    print("\nbin_area  n  con_codigo  cerca_GT  tasaGT")
    for a, b in bins:
        g = [d for d in dets if a <= d["area"] < b and "ocr" in d]
        if not g:
            continue
        cod = sum(bool(d["ocr"]["codigos"]) for d in g)
        gt = sum(any(cerca(c) for c, _ in d["ocr"]["codigos"]) for d in g)
        print(f"{a:5d}-{b:<5d} {len(g):3d} {cod:10d} {gt:9d}  "
              f"{gt / len(g) * 100:4.0f}%")

    if GT:
        print("\numbral  enviados  crops_GT  tasa  cubiertos(>=1)  "
              "cubiertos(>=2)")
        for T in [0, 3000, 5000, 8000, 10000, 12000, 14000, 16000]:
            sent = [d for d in dets if d["area"] >= T and "ocr" in d]
            gt_crops = [d for d in sent
                        if any(cerca(c) for c, _ in d["ocr"]["codigos"])]
            cub1 = set()
            for d in gt_crops:
                for c, _ in d["ocr"]["codigos"]:
                    for g in GT:
                        if ocr_codes._levenshtein(c, g) <= 2:
                            cub1.add(g)
            cub2 = {g for g in cub1
                    if sum(any(ocr_codes._levenshtein(c, g) <= 2
                               for c, _ in d["ocr"]["codigos"])
                           for d in gt_crops) >= 2}
            print(f"{T:6d} {len(sent):9d} {len(gt_crops):10d} "
                  f"{len(gt_crops) / len(sent) * 100:4.0f}% "
                  f"{len(cub1):13d} {len(cub2):14d}")


if __name__ == "__main__":
    main()
