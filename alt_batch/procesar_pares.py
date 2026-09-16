#!/usr/bin/env python3
"""Runner: fusiona los 75 pares cam1/cam2 (matching + codigo + sello) y
genera un PDF unificado por par en procesados/informes/."""
import argparse
import time

from fusionar_par import fusionar_par, pares_disponibles
from informe_par import generar_pdf


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--desde", type=int, default=0)
    p.add_argument("--max", type=int, default=None)
    args = p.parse_args()

    pares = pares_disponibles()[args.desde:]
    if args.max:
        pares = pares[:args.max]
    log = open("procesados/pares.log", "a")
    ok = fallos = 0
    t_ini = time.time()
    for i, par in enumerate(pares, 1):
        t0 = time.time()
        try:
            n = fusionar_par(par)
            generar_pdf(par)
            ok += 1
            log.write(f"[{i}/{len(pares)}] {par}: {n} camiones, "
                      f"{time.time() - t0:.1f}s\n")
        except Exception as e:
            fallos += 1
            log.write(f"[{i}/{len(pares)}] {par}: FALLO {e}\n")
        log.flush()
    log.write(f"TOTAL: {ok} ok, {fallos} fallos, "
              f"{time.time() - t_ini:.1f}s\n")
    log.close()
    print(f"TOTAL: {ok} ok, {fallos} fallos")


if __name__ == "__main__":
    main()
