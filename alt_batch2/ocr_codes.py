#!/usr/bin/env python3
"""Logica ISO 6346 portada de django/aduana/tasks.py (sin dependencias Django):
normalizacion posicional, reparacion de digito verificador, gate de sanidad,
votacion con consenso fuzzy. Reusa exactamente la logica de produccion."""
import re

_L2D = {"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "Z": "2",
        "S": "5", "G": "6", "B": "8"}
_D2L = {"0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "8": "B"}

LOC_RE = re.compile(r"<\|[A-Z]+_\d+\|>")


def limpiar(texto):
    """Quita tokens LOC del spotting y devuelve las lineas no vacias."""
    return [l.strip() for l in LOC_RE.sub("", texto or "").split("\n")
            if l.strip()]


def _compute_check_digit(code10):
    valores = {}
    n = 10
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if n % 11 == 0:
            n += 1
        valores[c] = n
        n += 1
    total = 0
    for i in range(10):
        ch = code10[i]
        v = valores[ch] if ch.isalpha() else int(ch)
        total += v * (2 ** i)
    d = total % 11
    return 0 if d == 10 else d


def es_contenedor_valido(codigo):
    if not isinstance(codigo, str):
        return False
    limpio = "".join(c.upper() for c in codigo if c.isalnum())
    if len(limpio) != 11 or not re.match(r"^[A-Z]{4}\d{7}$", limpio):
        return False
    if limpio[3] not in {"U", "J", "Z"}:
        return False
    return _compute_check_digit(limpio[:10]) == int(limpio[10])


def _es_formato_valido(code):
    limpio = re.sub(r"\s+", "", code.upper())
    return (len(limpio) == 11 and re.match(r"^[A-Z]{4}\d{7}$", limpio)
            and limpio[3] in {"U", "J", "Z"})


def _normalize_positions(seg):
    out = []
    changed = False
    for i, ch in enumerate(seg):
        if i < 4:
            if ch.isdigit() and ch in _D2L:
                ch = _D2L[ch]
                changed = True
        else:
            if ch.isalpha() and ch in _L2D:
                ch = _L2D[ch]
                changed = True
        out.append(ch)
    return "".join(out), changed


def _raw_skeleton_ok(raw):
    if len(raw) not in (10, 11):
        return False
    letters = sum(1 for ch in raw[:4] if ch.isalpha())
    digits = sum(1 for ch in raw[4:] if ch.isdigit())
    return letters >= 3 and digits >= len(raw) - 4 - 1


def _levenshtein(s1, s2):
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(
                prev[j + 1] + 1,
                curr[j] + 1,
                prev[j] + (0 if c1 == c2 else 1),
            ))
        prev = curr
    return prev[-1]


def _to_valid_code(seg):
    raw = seg
    if not _raw_skeleton_ok(raw):
        return None, False
    seg, normalized = _normalize_positions(seg)
    if not re.match(r"^[A-Z]{4}\d{6,7}$", seg):
        return None, False

    def accept(code, strict):
        if not strict and _levenshtein(raw, code) > 2:
            return None, False
        return code, strict
    if seg[3] not in "UJZ":
        if len(seg) == 11:
            for cat in "UJZ":
                cand = seg[:3] + cat + seg[4:]
                if es_contenedor_valido(cand):
                    return accept(cand, False)
        return None, False
    if len(seg) == 11:
        if not normalized and es_contenedor_valido(seg):
            return seg, True
        return accept(seg[:10] + str(_compute_check_digit(seg[:10])), False)
    return accept(seg + str(_compute_check_digit(seg)), False)


def extraer_codigos(textos):
    """Lista de (codigo, tier, segmento_crudo) a partir de textos OCR crudos."""
    out = []
    textos = list(dict.fromkeys(t for t in textos if t))
    if len(textos) > 1:
        textos.append(" ".join(textos))
    for t in textos:
        clean = re.sub(r"\s+", "", t.upper())
        for m in re.finditer(r"[A-Z0-9]{4}[0-9A-Z]{6,7}", clean):
            code, is_strict = _to_valid_code(m.group(0))
            if code:
                out.append((code, "strict" if is_strict else "repaired",
                            m.group(0)))
        for m in re.finditer(r"(\d{6})([A-Z]{4})", clean):
            owner, serial = m.group(2), m.group(1)
            if owner[3] not in "UJZ":
                continue
            code, _ = _to_valid_code(owner + serial)
            if code:
                out.append((code, "repaired", m.group(0)))
        for m in re.finditer(r"[A-Z]{4}\d{7}", clean):
            c = m.group(0)
            if c[3] in "UJZ":
                out.append((c, "raw", c))
    return out


def consenso(codigos, min_votos=2, max_distancia=2):
    """Voto por codigo con vecindario fuzzy (misma logica de produccion)."""
    counter = {}
    for c in codigos:
        code = re.sub(r"\s+", "", c.upper())
        if _es_formato_valido(code):
            counter[code] = counter.get(code, 0) + 1
    if not counter:
        return None
    unique = list(counter.keys())
    neighbors = {c: counter[c] for c in unique}
    for i in range(len(unique)):
        for j in range(i + 1, len(unique)):
            if _levenshtein(unique[i], unique[j]) <= max_distancia:
                neighbors[unique[i]] += counter[unique[j]]
                neighbors[unique[j]] += counter[unique[i]]
    best = max(neighbors, key=lambda c: (neighbors[c], counter[c]))
    if neighbors[best] < min_votos:
        return None
    return best


# --- ISO 6346 size & type codes (marcas de dimension/tipo pintadas junto
#     al codigo del contenedor, p.ej. 42G1, 45R1, 22G1) ---

SIZE_LENGTH = {
    "1": "10ft", "2": "20ft", "3": "30ft", "4": "40ft", "5": "45ft",
    "B": "24ft", "C": "24ft6in", "G": "41ft", "H": "43ft", "L": "45ft",
    "M": "48ft", "N": "49ft",
}
SIZE_HEIGHT = {
    "0": "8ft", "2": "8ft6in", "4": "9ft", "5": "9ft6in", "6": ">9ft6in",
    "8": "4ft3in", "9": "<=4ft",
}
TYPE_GROUP = {
    "G": "general purpose", "R": "reefer", "U": "open top", "T": "tank",
    "P": "flat/platform", "B": "bulk", "H": "insulated", "V": "ventilated",
    "S": "named cargo", "K": "special",
}

SIZE_TYPE_RE = re.compile(r"\b([0-9BCHLMN][0-9])([GRUTPBHVSK])(\d)?\b",
                          re.IGNORECASE)


def validar_tamano_tipo(codigo):
    """Valida un size/type code ISO 6346 (ej. '42G1', '45R1').

    Devuelve descripcion o None. Tolera la omision del subtipo ('42G').
    """
    m = SIZE_TYPE_RE.fullmatch(codigo.strip().upper())
    if not m:
        return None
    size, grupo, subtipo = m.group(1), m.group(2), m.group(3)
    if size[0] not in SIZE_LENGTH or size[1] not in SIZE_HEIGHT:
        return None
    desc = (f"{SIZE_LENGTH[size[0]]} {SIZE_HEIGHT[size[1]]} "
            f"{TYPE_GROUP[grupo]}")
    if size == "45" and grupo == "R":
        desc += " (high cube)"
    elif size[1] == "5":
        desc += " (high cube)"
    return desc + (f" subtipo {subtipo}" if subtipo else "")


def extraer_tamano_tipo(textos):
    """Size/type codes presentes en textos OCR, con su descripcion."""
    out = []
    for t in textos:
        for m in SIZE_TYPE_RE.finditer(t.upper()):
            desc = validar_tamano_tipo(m.group(0))
            if desc:
                out.append((m.group(0).upper(), desc))
    return list(dict.fromkeys(out))


def extraer_partiales(textos):
    """Lecturas incompletas del codigo: 3-4 letras + 4-7 alfanumericos
    (recorte de la etiqueta, con confusiones digito/letra). Devuelve strings
    de 11 chars con '#' en lo que falta.

    No participan en el checksum: son evidencia para fusion/reporting.
    """
    out = []
    _SIZE_PLAIN = re.compile(r"[0-9BCHLMN][0-9][GRUTPBHVSK]\d?")
    for t in textos:
        clean = re.sub(r"\s+", "", t.upper())
        # quitar size/type codes para no cortar el codigo al cruzarlos
        limpio = _SIZE_PLAIN.sub("", clean)
        for m in re.finditer(r"[A-Z]{3,4}[A-Z0-9]{4,8}", limpio):
            seg = m.group(0)
            owner, resto = seg[:4] if len(seg) > 4 else seg[:3], seg[4:] \
                if len(seg) > 4 else seg[3:]
            resto = resto[:7]
            digitos = ""
            n_dig = 0
            for c in resto:
                if c.isdigit():
                    digitos += c
                    n_dig += 1
                elif c in _L2D:
                    digitos += _L2D[c]
                    n_dig += 1
                else:
                    digitos += "#"
            if n_dig < 4:
                continue
            if len(owner) < 4:
                owner = owner + "#" * (4 - len(owner))
            if len(digitos) < 7:
                digitos = digitos + "#" * (7 - len(digitos))
            parcial = owner + digitos
            if "#" in parcial and parcial not in out:
                out.append(parcial)
        # solo digitos del serial junto a un size/type (codigo cerca)
        if SIZE_TYPE_RE.search(t.upper()):
            for m in re.finditer(r"\b([0-9]{5,7})\b", clean):
                d = m.group(1)
                if len(d) < 7:
                    parcial = "####" + d + "#" * (7 - len(d))
                    if parcial not in out:
                        out.append(parcial)
    return out
