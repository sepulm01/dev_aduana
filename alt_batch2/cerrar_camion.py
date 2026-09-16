#!/usr/bin/env python3
"""Cierre de camiones: sobre las pasadas cerradas por etapa_agrupar, empareja
las de ambas camaras (codigo + proximidad temporal), fusiona codigo y sello
en keypoint #3, elige la foto de alta calidad por camara y registra el
camion en estado.db (tabla camiones) + registros/<id>.json.

- Pasadas estacionadas: se intenta rescatar camiones que pasan adentro
  (division por codigo legible); el resto no se registra.
- Camara faltante confirmada (segmento nunca llego o fallo y el sucesor ya
  fue procesado) -> registro parcial."""
import argparse
import json
import os
import sqlite3
import time

import cv2

import estado
import ocr_codes
from foto_util import leer_frame_original
from pose_gate import PoseGate

TOL_ASOC = 1.0
MIN_VOTOS_SUB = 2


def _prio(tier):
    p = 0
    for t in tier.split(","):
        p = max(p, estado.PRIO.get(t, 0))
    if "noconfirmado" not in tier:
        p += 0.5
    return p


def _bd_video(video):
    return os.path.join(estado.PROCESADOS, video, "procesamiento.db")


def _meta_video(video):
    conn = sqlite3.connect(_bd_video(video))
    meta = {k: v for k, v in conn.execute("SELECT k, v FROM meta")}
    conn.close()
    return meta


def _ts_video(video):
    _, ts = estado.parse_nombre(video)
    return ts


def _t_global(video, frame):
    fps = float(_meta_video(video).get("fps", 20.0))
    return _ts_video(video) + frame / fps


def rangos_pasada(pid):
    conn = estado.conectar()
    rows = conn.execute(
        "SELECT video, idx, inicio, fin FROM pasada_rangos "
        "WHERE pasada_id=?", (pid,)).fetchall()
    conn.close()
    return rows


def codigos_rango(video, idx):
    conn = sqlite3.connect(_bd_video(video))
    rows = conn.execute(
        "SELECT codigo, tier, peso FROM codigos WHERE camion=? "
        "ORDER BY orden", (idx,)).fetchall()
    conn.close()
    return rows


def ocr_codigos_rango(video, idx):
    conn = sqlite3.connect(_bd_video(video))
    rows = conn.execute(
        "SELECT frame, codigo, tier FROM ocr_codigos WHERE camion=? "
        "ORDER BY frame", (idx,)).fetchall()
    conn.close()
    return rows


def _dividir_por_codigo(pid, cam, rangos):
    """Sub-unidades de una pasada por codigo: agrupa las lecturas
    (ocr_codigos, tier strict/repaired) en grupos de vecindario <=2
    (el mismo contenedor mal leido) y devuelve una sub-unidad por grupo
    con >=2 frames de respaldo. Sin grupos fuertes devuelve []."""
    eventos = []
    for video, idx, i, f in rangos:
        for frame, codigo, tier in ocr_codigos_rango(video, idx):
            if tier not in ("strict", "repaired"):
                continue
            eventos.append((_t_global(video, frame), video, idx, frame,
                            codigo, tier))
    if not eventos:
        return []
    codigos_unicos = sorted({e[4] for e in eventos})
    grupos = []
    for c in codigos_unicos:
        for g in grupos:
            if any(ocr_codes._levenshtein(c, cc) <= 2 for cc in g):
                g.append(c)
                break
        else:
            grupos.append([c])
    subs = []
    for g in grupos:
        frames_g = [e for e in eventos if e[4] in g]
        if len(frames_g) < MIN_VOTOS_SUB:
            continue
        best = max(g, key=lambda c: sum(1 for e in frames_g if e[4] == c))
        tier = ("strict" if any(e[5] == "strict" for e in frames_g)
                else "repaired")
        peso = len(frames_g)
        t0 = min(e[0] for e in frames_g)
        t1 = max(e[0] for e in frames_g)
        limites = {}
        for _, v, idx, f, _, _ in frames_g:
            lo, hi = limites.get((v, idx), (f, f))
            limites[(v, idx)] = (min(lo, f), max(hi, f))
        rangos_sub = []
        for (v, idx), (lo, hi) in sorted(limites.items()):
            r = next((rr for rr in rangos if rr[0] == v and rr[1] == idx),
                     None)
            if r is None:
                continue
            lo = max(lo - 20, r[2])
            hi = min(hi + 20, r[3])
            rangos_sub.append((v, idx, lo, hi))
        if not rangos_sub:
            continue
        subs.append({
            "cam": cam, "pid": pid,
            "t_inicio": max(t0 - 5, _ts_video(frames_g[0][1])),
            "t_fin": t1 + 5, "codigo_sub": best,
            "tier_sub": tier, "peso_sub": peso,
            "rangos": rangos_sub,
        })
    return subs


def cargar_unidades():
    """Todas las pasadas cerradas no consolidadas, como unidades.

    Una pasada con varios codigos fuertes se materializa en sub-unidades
    persistentes (tabla pasada_subs, una por contenedor); la pasada solo
    se consolida cuando todas sus sub-unidades estan resueltas."""
    conn = estado.conectar()
    pids = [r[0] for r in conn.execute(
        "SELECT id FROM pasadas WHERE estado='cerrada' AND consolidada=0 "
        "ORDER BY id")]
    conn.close()
    unidades = []
    for pid in pids:
        conn = estado.conectar()
        cam, t0, t1, est = conn.execute(
            "SELECT camara, t_inicio, t_fin, estacionada FROM pasadas "
            "WHERE id=?", (pid,)).fetchone()
        rangos = rangos_pasada(pid)
        filas_subs = conn.execute(
            "SELECT id, t_inicio, t_fin, codigo, tier, peso, fr FROM "
            "pasada_subs WHERE pasada_id=? AND consolidada=0",
            (pid,)).fetchall()
        if filas_subs:
            for sid, st0, st1, cod, tier, peso, fr in filas_subs:
                unidades.append({
                    "cam": cam, "pid": pid, "sub_id": sid,
                    "pasada_id": pid, "t_inicio": st0, "t_fin": st1,
                    "codigo_sub": cod, "tier_sub": tier,
                    "peso_sub": peso,
                    "rangos": [tuple(x) for x in json.loads(fr)],
                })
            conn.close()
            continue
        subs = _dividir_por_codigo(pid, cam, rangos)
        if subs and (est or len(subs) >= 2):
            for s in subs:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO pasada_subs (pasada_id, camara, t_inicio, "
                    "t_fin, codigo, tier, peso, fr) VALUES (?,?,?,?,?,?,?,?)",
                    (pid, cam, s["t_inicio"], s["t_fin"], s["codigo_sub"],
                     s["tier_sub"], s["peso_sub"], json.dumps(s["rangos"])))
                s["sub_id"] = cur.lastrowid
                s["pasada_id"] = pid
                unidades.append(s)
            conn.commit()
            conn.close()
            continue
        conn.close()
        if est:
            unidades.append({"pid": pid, "cam": cam, "estacionada": True,
                             "rangos": rangos, "subs": []})
        else:
            unidades.append({"pid": pid, "cam": cam, "t_inicio": t0,
                             "t_fin": t1, "rangos": rangos,
                             "estacionada": False})
    return unidades


def _camara_confirmada(cam, t0, t1):
    conn = estado.conectar()
    segs = conn.execute(
        "SELECT ts, estado FROM segmentos WHERE camara=? ORDER BY ts",
        (cam,)).fetchall()
    dur = 300.0
    solapes = [(ts, e) for ts, e in segs
               if ts <= t1 + 60 and ts + dur >= t0 - 60]
    abierta = conn.execute(
        "SELECT 1 FROM pasadas WHERE camara=? AND estado='abierta' AND "
        "t_inicio <= ? AND t_fin + ? >= ? LIMIT 1",
        (cam, t1 + estado.GAP_CONTINUIDAD, estado.GAP_CONTINUIDAD,
         t0)).fetchone()
    conn.close()
    if abierta:
        # hay una pasada de esa camara aun abierta que puede contener el
        # camion: no confirmar todavia
        return False
    if not solapes:
        for ts, e in segs:
            if ts >= t1 - 60:
                return e in ("listo", "error")
        return False
    return all(e in ("listo", "error") for _, e in solapes)


def _unir_por_codigo(nodos):
    parent = {i: i for i in range(len(nodos))}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # vecindario fuzzy: codigos con distancia <=2 son el mismo contenedor
    codigos = sorted({c for n in nodos for c in n["codigos_set"] if c})
    clusters = []
    for c in codigos:
        for cl in clusters:
            if any(ocr_codes._levenshtein(c, cc) <= 2 for cc in cl):
                cl.append(c)
                break
        else:
            clusters.append([c])
    por_cluster = {}
    for i, n in enumerate(nodos):
        for c in n["codigos_set"]:
            if not c:
                continue
            for cl in clusters:
                if c in cl:
                    por_cluster.setdefault(tuple(cl), []).append(i)
                    break
    for idxs in por_cluster.values():
        if len(idxs) > 1 and len({nodos[i]["cam"] for i in idxs}) > 1:
            for i in idxs[1:]:
                union(idxs[0], i)
    return parent


def _fusion_codigo(unidades):
    """Devuelve dict con codigo/tier/fuente/duda + mejores por camara."""
    mejores = {}
    for u in unidades:
        for codigo, tier, peso in u.get("codigos_por_rango", []):
            cand = (codigo, tier, peso)
            cur = mejores.get(u["cam"])
            if cur is None or (_prio(cand[1]), cand[2]) > \
                    (_prio(cur[1]), cur[2]):
                mejores[u["cam"]] = cand
    cams = sorted(mejores)
    res = {"cam1": None, "cam2": None, "codigo": None, "tier": None,
           "fuente": None, "duda": True}
    for cam in cams:
        res[f"cam{cam}"] = {"codigo": mejores[cam][0],
                            "tier": mejores[cam][1],
                            "peso": mejores[cam][2]}
    if not cams:
        return res
    if len(cams) == 1:
        cam = cams[0]
        if not mejores[cam][0]:
            res.update(codigo=None, tier=None, fuente=None, duda=True)
            return res
        res.update(codigo=mejores[cam][0], tier=mejores[cam][1],
                   fuente=f"cam{cam}",
                   duda="noconfirmado" in mejores[cam][1])
        return res
    a, b = cams
    ca, cb = mejores[a], mejores[b]
    if ca[0] == cb[0]:
        if not ca[0]:
            res.update(codigo=None, tier=None, fuente=None, duda=True)
            return res
        duda = ("noconfirmado" in ca[1] and "noconfirmado" in cb[1])
        tier = ca[1] if _prio(ca[1]) >= _prio(cb[1]) else cb[1]
        res.update(codigo=ca[0], tier=tier, fuente="ambas", duda=duda)
        return res
    pa, pb = _prio(ca[1]), _prio(cb[1])
    if pa == pb and ca[2] == cb[2]:
        res.update(codigo=None, tier="duda", fuente="ambas", duda=True)
        return res
    if (pa, ca[2]) > (pb, cb[2]):
        gan, gcam = ca, a
    else:
        gan, gcam = cb, b
    res.update(codigo=gan[0], tier=gan[1],
               fuente=f"cam{gcam} (otra cámara difiere)",
               duda="noconfirmado" in gan[1])
    return res


def _mejor_frame_sellos(video, idx, i, f):
    conn = sqlite3.connect(_bd_video(video))
    row = conn.execute(
        "SELECT frame, COUNT(*) FROM dets WHERE cls IN (0,1) AND frame "
        "BETWEEN ? AND ? GROUP BY frame ORDER BY COUNT(*) DESC, frame "
        "LIMIT 1", (i, f)).fetchone()
    if row:
        n = row[1]
    else:
        row2 = conn.execute(
            "SELECT frame FROM dets WHERE cls=3 AND frame BETWEEN ? AND ? "
            "GROUP BY frame ORDER BY COUNT(*) DESC, frame LIMIT 1",
            (i, f)).fetchone()
        if row2:
            row, n = (row2[0], 0), 0
    conn.close()
    return (row[0], n) if row else (None, 0)


def _foto_y_sello(cam, unidades, gate, camion_id):
    """Foto HQ anotada + sello en kpt3 para una camara del camion."""
    mejor = None
    for u in unidades:
        if u["cam"] != cam:
            continue
        for video, idx, i, f in u["rangos"]:
            frame, n = _mejor_frame_sellos(video, idx, i, f)
            if frame is None:
                continue
            if mejor is None or n > mejor[1]:
                mejor = (video, idx, frame, n)
    if mejor is None:
        return None
    video, idx, frame, n = mejor
    meta = _meta_video(video)
    delta = int(meta.get("proxy_offset", 0))
    cap = cv2.VideoCapture(os.path.join(estado.VIDEOS, video + ".mkv"))
    proxy_path = os.path.join(estado.VIDEOS, ".proxy",
                              video + "_h540.mp4")
    cap_proxy = cv2.VideoCapture(proxy_path) \
        if os.path.exists(proxy_path) else None
    img = leer_frame_original(cap, frame, delta, cap_proxy)
    cap.release()
    if cap_proxy is not None:
        cap_proxy.release()
    if img is None:
        return None
    w_img, h_img = img.shape[1], img.shape[0]
    conn = sqlite3.connect(_bd_video(video))
    dets_sellos = conn.execute(
        "SELECT cls, conf, x1, y1, x2, y2 FROM dets WHERE frame=? "
        "AND cls IN (0,1)", (frame,)).fetchall()
    dets_codigo = conn.execute(
        "SELECT conf, x1, y1, x2, y2 FROM dets WHERE frame=? AND cls=3 "
        "ORDER BY conf DESC LIMIT 3", (frame,)).fetchall()
    conn.close()

    n_con = n_sin = 0
    sellos_meta = []
    for cls, conf, x1, y1, x2, y2 in dets_sellos:
        color = (0, 200, 0) if cls == 0 else (0, 0, 255)
        cv2.rectangle(img, (int(x1 * w_img), int(y1 * h_img)),
                      (int(x2 * w_img), int(y2 * h_img)), color, 5)
        n_con += cls == 0
        n_sin += cls == 1
        sellos_meta.append({"cls": cls, "conf": round(conf, 3),
                            "bbox": [round(x1, 4), round(y1, 4),
                                     round(x2, 4), round(y2, 4)]})
    codigo_meta = []
    for conf, x1, y1, x2, y2 in dets_codigo:
        cv2.rectangle(img, (int(x1 * w_img), int(y1 * h_img)),
                      (int(x2 * w_img), int(y2 * h_img)), (0, 0, 255), 10)
        codigo_meta.append({"conf": round(conf, 3),
                            "bbox": [round(x1, 4), round(y1, 4),
                                     round(x2, 4), round(y2, 4)]})

    # pose -> keypoint #3 (indice 2)
    sello_res = {"cls": None, "conf": None, "kpt3": None}
    try:
        dets_pose = gate.detect(img)
        if dets_pose is not None and len(dets_pose):
            mejor_pose = max(dets_pose, key=lambda d: float(
                gate.kpts_de(d)[2][2]))
            kpts = gate.kpts_de(mejor_pose)
            nx, ny = float(kpts[2][0]) / w_img, float(kpts[2][1]) / h_img
            k3c = float(kpts[2][2])
            sello_res["kpt3"] = {"x": round(nx, 4), "y": round(ny, 4),
                                 "conf": round(k3c, 3)}
            best = None
            for cls, conf, x1, y1, x2, y2 in dets_sellos:
                bw, bh = x2 - x1, y2 - y1
                ex1, ey1 = x1 - TOL_ASOC * bw, y1 - TOL_ASOC * bh
                ex2, ey2 = x2 + TOL_ASOC * bw, y2 + TOL_ASOC * bh
                if not (ex1 <= nx <= ex2 and ey1 <= ny <= ey2):
                    continue
                cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                dist = ((nx - cx) ** 2 + (ny - cy) ** 2) ** 0.5
                if best is None or dist < best[0] - 1e-9 or \
                        (abs(dist - best[0]) < 1e-9 and conf > best[1]):
                    best = (dist, conf, cls)
            if best:
                sello_res.update(cls=best[2], conf=round(best[1], 3))
    except Exception as e:
        print(f"  pose fallo (cam{cam}): {e}")

    os.makedirs(estado.FOTOS, exist_ok=True)
    foto_path = os.path.join(estado.FOTOS, f"{camion_id}_cam{cam}.jpg")
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if ok:
        with open(foto_path, "wb") as fh:
            fh.write(buf.tobytes())

    return {
        "video": video, "rango": idx, "frame": frame,
        "foto": os.path.relpath(foto_path, estado.BASE),
        "n_con": n_con, "n_sin": n_sin,
        "detecciones": {"sellos": sellos_meta, "codigo": codigo_meta},
        "sello": sello_res,
    }


def _veredicto_sello(r1, r2):
    res = {"veredicto": "sin datos", "cls": None, "conf": None,
           "duda": True, "detalle": None}
    if r1 and r1.get("cls") is not None and r2 and r2.get("cls") is not None:
        cls1, cls2 = r1["cls"], r2["cls"]
        c1, c2 = r1["conf"], r2["conf"]
        if cls1 == cls2:
            cmin = min(c1, c2)
            texto = estado.CLS_TEXTO[cls1]
            res.update(cls=cls1, conf=cmin)
            if cmin < estado.CONF_SELLO:
                cam_baja = "cam1" if c1 <= c2 else "cam2"
                res.update(veredicto=texto, duda=True,
                           detalle=f"{texto} (duda: conf baja en {cam_baja})")
            else:
                res.update(veredicto=texto, duda=False,
                           detalle=f"{texto} (ambas cámaras)")
        else:
            res.update(veredicto="DUDA", duda=True,
                       detalle=(f"cam1 {estado.CLS_TEXTO[cls1]} ({c1:.2f}) "
                                f"/ cam2 {estado.CLS_TEXTO[cls2]} ({c2:.2f})"))
    else:
        r = r1 if (r1 and r1.get("cls") is not None) else \
            (r2 if (r2 and r2.get("cls") is not None) else None)
        if r:
            cam = "cam1" if r is r1 else "cam2"
            texto = estado.CLS_TEXTO[r["cls"]]
            res.update(cls=r["cls"], conf=r["conf"])
            if r["conf"] < estado.CONF_SELLO:
                res.update(veredicto=texto, duda=True,
                           detalle=(f"{texto} (solo {cam}, conf baja "
                                    f"{r['conf']:.2f})"))
            else:
                res.update(veredicto=texto, duda=False,
                           detalle=f"{texto} (solo {cam})")
    return res


def _registrar(grupo, gate, conn):
    camiones_u = [u for u in grupo]
    t0 = min(u["t_inicio"] for u in camiones_u)
    t1 = max(u["t_fin"] for u in camiones_u)
    cod = _fusion_codigo(camiones_u)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO camiones (t_inicio, t_fin, codigo, tier, fuente, "
        "duda_codigo, creado_at) VALUES (?,?,?,?,?,?,?)",
        (t0, t1, cod["codigo"], cod["tier"], cod["fuente"],
         int(cod["duda"]), time.time()))
    camion_id = cur.lastrowid

    f1 = _foto_y_sello(1, camiones_u, gate, camion_id)
    f2 = _foto_y_sello(2, camiones_u, gate, camion_id)
    sell = _veredicto_sello(
        f1["sello"] if f1 else None, f2["sello"] if f2 else None)
    parcial = int(not f1 or not f2)
    cur.execute(
        "UPDATE camiones SET seal3=?, seal3_conf=?, seal3_veredicto=?, "
        "duda_sello=?, foto_cam1=?, foto_cam2=?, parcial=? WHERE id=?",
        (sell["cls"], sell["conf"], sell["veredicto"], int(sell["duda"]),
         f1["foto"] if f1 else None, f2["foto"] if f2 else None,
         parcial, camion_id))

    registro = {
        "id": camion_id, "t_inicio": t0, "t_fin": t1,
        "parcial": bool(parcial),
        "codigo": {"codigo": cod["codigo"], "tier": cod["tier"],
                   "fuente": cod["fuente"], "duda": cod["duda"],
                   "cam1": cod["cam1"], "cam2": cod["cam2"]},
        "sello_kpt3": sell,
        "camaras": {},
        "rangos": {"cam1": sorted({f"{v}:{i}" for u in camiones_u
                                   if u["cam"] == 1
                                   for v, i, _, _ in u["rangos"]}),
                   "cam2": sorted({f"{v}:{i}" for u in camiones_u
                                   if u["cam"] == 2
                                   for v, i, _, _ in u["rangos"]})},
    }
    if f1:
        registro["camaras"]["cam1"] = f1
    if f2:
        registro["camaras"]["cam2"] = f2
    os.makedirs(estado.REGISTROS, exist_ok=True)
    reg_path = os.path.join(estado.REGISTROS, f"{camion_id}.json")
    with open(reg_path, "w") as fh:
        json.dump(registro, fh, indent=2, default=str)
    cur.execute("UPDATE camiones SET registro=? WHERE id=?",
                (os.path.relpath(reg_path, estado.BASE), camion_id))
    for u in camiones_u:
        if u.get("sub_id") is not None:
            cur.execute(
                "UPDATE pasada_subs SET consolidada=1, camion_id=? "
                "WHERE id=?", (camion_id, u["sub_id"]))
        elif u.get("pid") is not None:
            cur.execute(
                "UPDATE pasadas SET consolidada=1, camion_id=? WHERE id=?",
                (camion_id, u["pid"]))
    for pid in {u["pasada_id"] for u in camiones_u
                if u.get("pasada_id") is not None}:
        pend = cur.execute(
            "SELECT 1 FROM pasada_subs WHERE pasada_id=? AND consolidada=0 "
            "LIMIT 1", (pid,)).fetchone()
        if not pend:
            cur.execute(
                "UPDATE pasadas SET consolidada=1 WHERE id=? "
                "AND consolidada=0", (pid,))
    conn.commit()
    print(f"  camion {camion_id} registrado: codigo={cod['codigo']} "
          f"({cod['fuente']}) sello={sell['veredicto']} parcial={parcial}")
    return camion_id


def correr():
    estado.crear_tablas()
    with estado.FileLock("cierre"):
        return _correr_lock()
def _correr_lock():
    unidades = cargar_unidades()
    if not unidades:
        return 0

    registrables = []
    estacionadas_resto = []
    for u in unidades:
        if u.get("estacionada"):
            estacionadas_resto.append(u)
        elif u.get("codigo_sub"):
            u["codigos_set"] = {u["codigo_sub"]}
            u["codigos_por_rango"] = [(u["codigo_sub"],
                                       u.get("tier_sub", "repaired"),
                                       u.get("peso_sub", 2))]
            registrables.append(u)
        else:
            u["codigos_set"] = {
                c for video, idx, _, _ in u["rangos"]
                for c, _, _ in codigos_rango(video, idx)}
            u["codigos_por_rango"] = [
                (c, t, p) for video, idx, _, _ in u["rangos"]
                for c, t, p in codigos_rango(video, idx)]
            registrables.append(u)
    for s in estacionadas_resto:
        conn = estado.conectar()
        conn.execute("UPDATE pasadas SET consolidada=1 WHERE id=?",
                     (s["pid"],))
        conn.commit()
        conn.close()

    nodos = registrables

    parent = _unir_por_codigo(nodos)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    sobr1 = [i for i, n in enumerate(nodos) if n["cam"] == 1]
    sobr2 = [i for i, n in enumerate(nodos) if n["cam"] == 2]
    usados = set()
    for i2 in sorted(sobr2, key=lambda i: nodos[i]["t_inicio"]):
        best = None
        for i1 in sobr1:
            if i1 in usados:
                continue
            d = abs(nodos[i1]["t_inicio"] - nodos[i2]["t_inicio"])
            if d <= estado.TOL_MATCH and (best is None or d < best[0]):
                best = (d, i1)
        if best:
            union(i2, best[1])
            usados.add(best[1])

    grupos = {}
    for i in range(len(nodos)):
        grupos.setdefault(find(i), []).append(nodos[i])

    gate = None
    conn = estado.conectar()
    hechas = 0
    with estado.GpuLock():
        for g in grupos.values():
            cams = {u["cam"] for u in g}
            t0 = min(u["t_inicio"] for u in g)
            t1 = max(u["t_fin"] for u in g)
            if len(cams) == 1:
                faltante = 2 if 1 in cams else 1
                if not _camara_confirmada(faltante, t0, t1):
                    continue
            if gate is None:
                gate = PoseGate()
                gate.warmup()
            _registrar(g, gate, conn)
            hechas += 1
    conn.close()
    return hechas


def rearmar_afectados():
    """Re-arma solo los camiones cuyas pasadas se dividen por codigo:
    agrupa todas las pasadas cerradas (codigo + tiempo), encuentra los
    grupos con pasadas divisibles y resetea esos grupos (camiones,
    sub-unidades y consolidacion) para que correr() los re-registre."""
    estado.crear_tablas()
    conn = estado.conectar()
    rows = conn.execute(
        "SELECT id, camara, t_inicio, t_fin, estacionada FROM pasadas "
        "WHERE estado='cerrada'").fetchall()
    nodos = []
    for pid, cam, t0, t1, est in rows:
        rangos = rangos_pasada(pid)
        subs_prev = [r[0] for r in conn.execute(
            "SELECT codigo FROM pasada_subs WHERE pasada_id=?", (pid,))]
        if subs_prev:
            nodos.append({"pid": pid, "cam": cam, "t_inicio": t0,
                          "t_fin": t1, "codigos_set": set(subs_prev),
                          "afectada": True})
            continue
        nuevos = _dividir_por_codigo(pid, cam, rangos)
        if nuevos and (est or len(nuevos) >= 2):
            nodos.append({"pid": pid, "cam": cam, "t_inicio": t0,
                          "t_fin": t1,
                          "codigos_set": {s["codigo_sub"] for s in nuevos},
                          "afectada": True})
        else:
            cods = {c for video, idx, _, _ in rangos
                    for c, _, _ in codigos_rango(video, idx)}
            nodos.append({"pid": pid, "cam": cam, "t_inicio": t0,
                          "t_fin": t1, "codigos_set": cods,
                          "afectada": False})
    conn.close()

    parent = _unir_por_codigo(nodos)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    sobr1 = [i for i, n in enumerate(nodos) if n["cam"] == 1]
    sobr2 = [i for i, n in enumerate(nodos) if n["cam"] == 2]
    usados = set()
    for i2 in sorted(sobr2, key=lambda i: nodos[i]["t_inicio"]):
        best = None
        for i1 in sobr1:
            if i1 in usados:
                continue
            d = abs(nodos[i1]["t_inicio"] - nodos[i2]["t_inicio"])
            if d <= estado.TOL_MATCH and (best is None or d < best[0]):
                best = (d, i1)
        if best:
            union(i2, best[1])
            usados.add(best[1])

    grupos = {}
    for i in range(len(nodos)):
        grupos.setdefault(find(i), []).append(nodos[i])

    afectados = set()
    for g in grupos.values():
        if any(n.get("afectada") for n in g):
            afectados.update(n["pid"] for n in g)
    if not afectados:
        print("rearmar: sin pasadas afectadas")
        return 0

    conn = estado.conectar()
    marca = ",".join("?" * len(afectados))
    cids = [r[0] for r in conn.execute(
        f"SELECT DISTINCT camion_id FROM pasadas WHERE id IN ({marca}) "
        f"AND camion_id IS NOT NULL", tuple(afectados))]
    cids += [r[0] for r in conn.execute(
        f"SELECT DISTINCT camion_id FROM pasada_subs "
        f"WHERE pasada_id IN ({marca}) AND camion_id IS NOT NULL",
        tuple(afectados))]
    cids = sorted(set(cids))
    for cid in cids:
        reg = conn.execute("SELECT registro FROM camiones WHERE id=?",
                           (cid,)).fetchone()
        if reg and reg[0]:
            try:
                os.remove(os.path.join(estado.BASE, reg[0]))
            except OSError:
                pass
    if cids:
        marcac = ",".join("?" * len(cids))
        conn.execute(f"DELETE FROM camiones WHERE id IN ({marcac})",
                     tuple(cids))
    conn.execute(f"DELETE FROM pasada_subs WHERE pasada_id IN ({marca})",
                 tuple(afectados))
    conn.execute(
        f"UPDATE pasadas SET consolidada=0, camion_id=NULL "
        f"WHERE id IN ({marca})", tuple(afectados))
    conn.commit()
    conn.close()
    print(f"rearmar: {len(afectados)} pasadas reseteadas, "
          f"{len(cids)} camiones borrados")
    return len(afectados)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--solo-afectados", action="store_true",
                   help="resetear y re-registrar solo camiones con pasadas "
                        "divisibles por codigo")
    args = p.parse_args()
    t0 = time.time()
    if args.solo_afectados:
        rearmar_afectados()
    n = correr()
    print(f"cierre: {n} camiones registrados ({time.time() - t0:.1f}s)")


if __name__ == "__main__":
    main()
