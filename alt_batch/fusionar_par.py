#!/usr/bin/env python3
"""Fusion de par cam1/cam2 de un mismo tramo horario:

1. matching de camiones entre camaras (por codigo ISO y por centroide
   temporal de la pasada),
2. codigo fusionado (resolucion automatica por tier y peso, complemento
   entre camaras),
3. veredicto de sello en cierre #3 (desacuerdo = DUDA siempre).

Resultado en procesados/pares.db (tablas camiones y cierres)."""
import argparse
import glob
import os
import sqlite3

from foto_util import foto_frame

FPS = 20.0
GAP_TEMPORAL = 10.0
CONF_SELLO = 0.6

PRIO = {"strict": 3, "repaired": 2, "raw": 1}
CLS_TEXTO = {0: "CON SELLO", 1: "SIN SELLO", -1: "sin identificar"}


def cargar_video(nombre):
    db = os.path.join("procesados", nombre, "procesamiento.db")
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    data = {
        "nombre": nombre,
        "rangos": {r[0]: (r[1], r[2]) for r in cur.execute(
            "SELECT idx, inicio, fin FROM rangos")},
        "codigos": {},
        "pose": {},
        "conn": conn,
        "cur": cur,
    }
    for r in cur.execute(
            "SELECT camion, orden, codigo, tier, peso, frames, parciales, "
            "size_type FROM codigos ORDER BY camion, orden"):
        data["codigos"].setdefault(r[0], []).append(r)
    for r in cur.execute(
            "SELECT camion, cierre, frame, sello_cls, sello_conf "
            "FROM pose_cierres ORDER BY camion, cierre"):
        data["pose"].setdefault(r[0], []).append(r[1:])
    return data


def cerrar_video(v):
    v["conn"].close()


def _centro(v1, v2, cam, rango):
    r0, r1 = (v1 if cam == 1 else v2)["rangos"][rango]
    return (r0 + r1) / 2 / FPS


def matchear(v1, v2):
    """Clusters de (cam, rango) que representan al mismo camion."""
    parent = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for cam, v in ((1, v1), (2, v2)):
        for r in v["rangos"]:
            parent[(cam, r)] = (cam, r)
    mapa = {}
    for cam, v in ((1, v1), (2, v2)):
        for r, rows in v["codigos"].items():
            for row in rows:
                mapa.setdefault(row[2], []).append((cam, r))
    for code, items in mapa.items():
        if any(c == 1 for c, _ in items) and any(c == 2 for c, _ in items):
            for k in items[1:]:
                union(items[0], k)

    def grupos():
        g = {}
        for k in parent:
            g.setdefault(find(k), []).append(k)
        return list(g.values())

    sob1 = sorted([g for g in grupos() if all(c == 1 for c, _ in g)],
                  key=lambda g: _centro(v1, v2, *g[0]))
    sob2 = sorted([g for g in grupos() if all(c == 2 for c, _ in g)],
                  key=lambda g: _centro(v1, v2, *g[0]))
    usados = set()
    for g2 in sob2:
        t2 = _centro(v1, v2, *g2[0])
        best = None
        for gi, g1 in enumerate(sob1):
            if gi in usados:
                continue
            d = abs(_centro(v1, v2, *g1[0]) - t2)
            if d <= GAP_TEMPORAL and (best is None or d < best[0]):
                best = (d, gi, g1)
        if best:
            union(best[2][0], g2[0])
            usados.add(best[1])

    trucks = sorted(grupos(), key=lambda g: min(_centro(v1, v2, *k)
                                                for k in g))
    return trucks


def _prio(tier):
    p = 0
    for t in tier.split(","):
        p = max(p, PRIO.get(t, 0))
    if "noconfirmado" not in tier:
        p += 0.5
    return p


def fusionar_codigo(nodes, v1, v2):
    """Devuelve dict: codigo, tier, fuente, duda,
    cam1_codigo, cam1_tier, cam2_codigo, cam2_tier."""
    filas = []
    for cam, r in nodes:
        v = v1 if cam == 1 else v2
        rows = v["codigos"].get(r, [])
        if rows:
            filas.append((cam, r, rows[0]))
    if not filas:
        return {"codigo": None, "tier": None, "fuente": None, "duda": True,
                "cam1_codigo": None, "cam1_tier": None,
                "cam2_codigo": None, "cam2_tier": None}
    filas.sort(key=lambda f: (-_prio(f[2][3]), -f[2][4], f[0]))
    cam1 = next((f[2] for f in filas if f[0] == 1), None)
    cam2 = next((f[2] for f in filas if f[0] == 2), None)
    mejor = filas[0]
    cam, row = mejor[0], mejor[2]
    otras = [f for f in filas if f[0] != cam]
    if not otras:
        duda = "noconfirmado" in row[3]
        return {"codigo": row[2], "tier": row[3], "fuente": f"cam{cam}",
                "duda": duda,
                "cam1_codigo": cam1[2] if cam1 else None,
                "cam1_tier": cam1[3] if cam1 else None,
                "cam2_codigo": cam2[2] if cam2 else None,
                "cam2_tier": cam2[3] if cam2 else None}
    otra = otras[0][2]
    if otra[2] == row[2]:
        duda = ("noconfirmado" in row[3]
                and "noconfirmado" in otra[3])
        return {"codigo": row[2], "tier": row[3], "fuente": "ambas",
                "duda": duda,
                "cam1_codigo": cam1[2] if cam1 else None,
                "cam1_tier": cam1[3] if cam1 else None,
                "cam2_codigo": cam2[2] if cam2 else None,
                "cam2_tier": cam2[3] if cam2 else None}
    p1, p2 = _prio(row[3]), _prio(otra[3])
    if p1 == p2 and row[4] == otra[4]:
        return {"codigo": None, "tier": "duda", "fuente": "ambas",
                "duda": True,
                "cam1_codigo": cam1[2] if cam1 else None,
                "cam1_tier": cam1[3] if cam1 else None,
                "cam2_codigo": cam2[2] if cam2 else None,
                "cam2_tier": cam2[3] if cam2 else None}
    if p1 > p2 or (p1 == p2 and row[4] > otra[4]):
        gan = mejor
    else:
        gan = otras[0]
    gcam, grow = gan[0], gan[2]
    return {"codigo": grow[2], "tier": grow[3],
            "fuente": f"cam{gcam} (otra cámara difiere)",
            "duda": "noconfirmado" in grow[3],
            "cam1_codigo": cam1[2] if cam1 else None,
            "cam1_tier": cam1[3] if cam1 else None,
            "cam2_codigo": cam2[2] if cam2 else None,
            "cam2_tier": cam2[3] if cam2 else None}


def fusionar_cierres(nodes, v1, v2):
    """Devuelve lista por cierre: dict(cierre, cls, conf, veredicto,
    detalle, duda, cam1_*, cam2_*)."""
    acc = {1: {}, 2: {}}
    for cam, r in nodes:
        v = v1 if cam == 1 else v2
        for cierre, frame, cls, conf in v["pose"].get(r, []):
            cur = acc[cam].get(cierre)
            if cur is None or conf > cur[1]:
                acc[cam][cierre] = (frame, cls, conf)
    out = []
    for cierre in sorted(set(acc[1]) | set(acc[2])):
        r1, r2 = acc[1].get(cierre), acc[2].get(cierre)
        res = {"cierre": cierre, "cls": None, "conf": None,
               "cam1_cls": None, "cam1_conf": None, "cam1_frame": None,
               "cam2_cls": None, "cam2_conf": None, "cam2_frame": None}
        if r1:
            res.update(cam1_frame=r1[0], cam1_cls=r1[1], cam1_conf=r1[2])
        if r2:
            res.update(cam2_frame=r2[0], cam2_cls=r2[1], cam2_conf=r2[2])
        if r1 and r2:
            if r1[1] == r2[1]:
                cmin = min(r1[2], r2[2])
                res.update(cls=r1[1], conf=cmin)
                if r1[1] == -1:
                    res.update(veredicto="sin identificar", duda=True,
                               detalle=f"cam1 {r1[2]:.2f} / cam2 {r2[2]:.2f}")
                elif cmin < CONF_SELLO:
                    cam_baja = "cam1" if r1[2] <= r2[2] else "cam2"
                    res.update(veredicto=CLS_TEXTO[r1[1]], duda=True,
                               detalle=(f"{CLS_TEXTO[r1[1]]} (duda: conf "
                                        f"baja en {cam_baja})"))
                else:
                    res.update(veredicto=CLS_TEXTO[r1[1]], duda=False,
                               detalle=f"{CLS_TEXTO[r1[1]]} (ambas cámaras)")
            else:
                res.update(veredicto="DUDA", duda=True,
                           detalle=(f"cam1 {CLS_TEXTO[r1[1]]} ({r1[2]:.2f}) "
                                    f"/ cam2 {CLS_TEXTO[r2[1]]} ({r2[2]:.2f})"))
        else:
            r = r1 or r2
            cam = "cam1" if r1 else "cam2"
            res.update(cls=r[1], conf=r[2])
            if r[1] == -1:
                res.update(veredicto="sin identificar", duda=True,
                           detalle=f"solo {cam} ({r[2]:.2f})")
            elif r[2] < CONF_SELLO:
                res.update(veredicto=CLS_TEXTO[r[1]], duda=True,
                           detalle=(f"{CLS_TEXTO[r[1]]} (solo {cam}, conf "
                                    f"baja {r[2]:.2f})"))
            else:
                res.update(veredicto=CLS_TEXTO[r[1]], duda=False,
                           detalle=f"{CLS_TEXTO[r[1]]} (solo {cam})")
        out.append(res)
    return out


def _foto_cam(cam, rangos_cam, v):
    cur = v["cur"]
    best = None
    for r in rangos_cam:
        r0, r1 = v["rangos"][r]
        csvs = [row[5] for row in v["codigos"].get(r, [])]
        f = foto_frame(cur, r, r0, r1, csvs)
        if f is None:
            continue
        cnt = cur.execute("SELECT COUNT(*) FROM dets WHERE cls IN (0,1) "
                          "AND frame=?", (f,)).fetchone()[0]
        if best is None or cnt > best[1]:
            best = (f, cnt)
    return best


def fusionar_par(par):
    """Fusiona el par y escribe sus filas en procesados/pares.db.
    Devuelve el numero de camiones fusionados."""
    v1 = cargar_video(f"cam1_{par}")
    v2 = cargar_video(f"cam2_{par}")
    trucks = matchear(v1, v2)

    db = os.path.join("procesados", "pares.db")
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS camiones (
        par TEXT, idx INTEGER,
        cam1_rangos TEXT, cam2_rangos TEXT,
        t0 REAL, t1 REAL,
        codigo TEXT, tier TEXT, fuente TEXT, duda_codigo INTEGER,
        cam1_codigo TEXT, cam1_tier TEXT, cam2_codigo TEXT, cam2_tier TEXT,
        cam1_frame INTEGER, cam2_frame INTEGER,
        PRIMARY KEY (par, idx))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS cierres (
        par TEXT, idx INTEGER, cierre INTEGER,
        seal3 INTEGER, seal3_conf REAL, veredicto TEXT, detalle TEXT,
        duda INTEGER,
        cam1_cls INTEGER, cam1_conf REAL, cam2_cls INTEGER, cam2_conf REAL,
        cam1_frame INTEGER, cam2_frame INTEGER,
        PRIMARY KEY (par, idx, cierre))""")
    cur.execute("DELETE FROM camiones WHERE par=?", (par,))
    cur.execute("DELETE FROM cierres WHERE par=?", (par,))

    for idx, nodes in enumerate(trucks, 1):
        ts = sorted(_centro(v1, v2, *k) for k in nodes)
        r1 = ",".join(str(r) for c, r in sorted(nodes) if c == 1)
        r2 = ",".join(str(r) for c, r in sorted(nodes) if c == 2)
        cod = fusionar_codigo(nodes, v1, v2)
        f1 = _foto_cam(1, [r for c, r in nodes if c == 1], v1)
        f2 = _foto_cam(2, [r for c, r in nodes if c == 2], v2)
        cur.execute(
            "INSERT INTO camiones VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (par, idx, r1, r2, ts[0], ts[-1],
             cod["codigo"], cod["tier"], cod["fuente"],
             int(cod["duda"]),
             cod["cam1_codigo"], cod["cam1_tier"],
             cod["cam2_codigo"], cod["cam2_tier"],
             f1[0] if f1 else None, f2[0] if f2 else None))
        for res in fusionar_cierres(nodes, v1, v2):
            cur.execute(
                "INSERT INTO cierres VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (par, idx, res["cierre"], res["cls"], res["conf"],
                 res["veredicto"], res["detalle"], int(res["duda"]),
                 res["cam1_cls"], res["cam1_conf"],
                 res["cam2_cls"], res["cam2_conf"],
                 res["cam1_frame"], res["cam2_frame"]))
    conn.commit()
    conn.close()
    cerrar_video(v1)
    cerrar_video(v2)
    return len(trucks)


def pares_disponibles():
    pares = set()
    for f in glob.glob("videos/cam1_*.mkv"):
        pares.add(os.path.basename(f)[5:-4])
    for f in glob.glob("videos/cam2_*.mkv"):
        pares.add(os.path.basename(f)[5:-4])
    return sorted(pares)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--par", required=True)
    args = p.parse_args()
    n = fusionar_par(args.par)
    print(f"{args.par}: {n} camiones fusionados")


if __name__ == "__main__":
    main()
