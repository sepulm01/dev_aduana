"""
Lógica pura (sin ORM) para extraer y validar códigos de contenedor a partir
de resultados de OCR. Separado de aduana.tasks para poder testear sin DB.
"""
import re
from collections import defaultdict

LETRA_A_DIGITO = {
    "O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2",
    "S": "5", "G": "6", "B": "8", "A": "4", "T": "7",
}
DIGITO_A_LETRA = {
    "0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "8": "B", "4": "A",
}

_CODIGO_RE = re.compile(r"^[A-Z]{4}\d{7}$")

_VALORES_LETRA = {}
_n = 10
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    if _n % 11 == 0:
        _n += 1
    _VALORES_LETRA[_c] = _n
    _n += 1


def es_contenedor_valido(contenedor):
    if not isinstance(contenedor, str):
        return False

    limpio = "".join(c.upper() for c in contenedor if c.isalnum())

    if len(limpio) != 11:
        return False

    if not _CODIGO_RE.match(limpio):
        return False

    if limpio[3] not in {"U", "J", "Z"}:
        return False

    total = 0
    for i in range(10):
        char = limpio[i]
        valor = _VALORES_LETRA[char] if char.isalpha() else int(char)
        total += valor * (2 ** i)

    checksum = total % 11
    if checksum == 10:
        checksum = 0

    return checksum == int(limpio[10])


def corregir_posicional(s):
    """
    Corrige confusiones típicas de OCR en un string de 11 caracteres:
    posiciones 0-3 deben ser letras (dígito -> letra), posiciones 4-10
    deben ser dígitos (letra -> dígito). Devuelve el string corregido si
    matchea el patrón de código de contenedor, o None si no aplica.
    """
    if len(s) != 11:
        return None

    out = []
    for i, ch in enumerate(s):
        if i < 4:
            out.append(DIGITO_A_LETRA.get(ch, ch) if ch.isdigit() else ch)
        else:
            out.append(LETRA_A_DIGITO.get(ch, ch) if ch.isalpha() else ch)
    fixed = "".join(out)

    if _CODIGO_RE.match(fixed):
        return fixed
    return None


def ventanas_11(texto):
    """
    Limpia el texto (solo alfanuméricos, uppercase) y devuelve el set de
    todas las ventanas deslizantes de 11 caracteres.
    """
    limpio = "".join(c.upper() for c in texto if c.isalnum())
    if len(limpio) < 11:
        return set()
    return {limpio[i:i + 11] for i in range(len(limpio) - 10)}


def _textos_fuente(regions, ocr_text):
    """
    Arma la lista de textos "fuente" desde los que extraer ventanas de 11
    caracteres: cada región individual, y las concatenaciones de todas las
    regiones en 3 órdenes (almacenamiento, X, Y), más ocr_text si viene.
    """
    textos = []

    for r in regions:
        if len(r) >= 1 and isinstance(r[0], str) and r[0]:
            textos.append(r[0])

    if regions:
        # Orden de almacenamiento
        textos.append("".join(r[0] for r in regions if isinstance(r[0], str)))

        # Orden por X e Y, solo regiones con bbox no vacío
        con_bbox = [r for r in regions if len(r) >= 3 and r[2]]
        if con_bbox:
            por_x = sorted(con_bbox, key=lambda r: r[2][0][0])
            textos.append("".join(r[0] for r in por_x if isinstance(r[0], str)))

            por_y = sorted(con_bbox, key=lambda r: r[2][0][1])
            textos.append("".join(r[0] for r in por_y if isinstance(r[0], str)))

    if ocr_text:
        textos.append(ocr_text)

    return textos


def texto_tiene_codigo_valido(texto):
    """
    True si alguna ventana deslizante de 11 caracteres extraída de `texto`
    es un código de contenedor válido, directamente o tras corregir
    confusiones posicionales típicas de OCR (letra<->dígito).
    """
    for ventana in ventanas_11(texto):
        if es_contenedor_valido(ventana):
            return True
        corregido = corregir_posicional(ventana)
        if corregido and es_contenedor_valido(corregido):
            return True
    return False


_MAX_PREFIJOS = 100
_MAX_CUERPOS = 300


def consenso_parcial(textos):
    """
    Reconstruye un código de contenedor combinando fragmentos parciales de
    distintas lecturas OCR de un mismo evento (p.ej. una detección aporta
    solo el prefijo de 4 letras — crop horizontal —, otra solo los 7
    dígitos — crop vertical —). Es una red de seguridad para cuando ningún
    texto individual contiene por sí solo un código completo y válido
    (`candidatos_de_regiones` no encontró nada).

    Entrada: lista de strings (lecturas OCR crudas). Devuelve el código
    reconstruido (str) si hay un candidato ganador sin ambigüedad, o None
    si no hay candidatos válidos o si hay un empate en el máximo soporte
    (mejor no asignar un código posiblemente erróneo).
    """
    prefijos = defaultdict(set)  # prefijo de 4 letras -> índices de texto que lo aportan
    cuerpos = defaultdict(set)   # cuerpo de 7 dígitos -> índices de texto que lo aportan

    for idx, texto in enumerate(textos or []):
        limpio = "".join(c.upper() for c in (texto or "") if c.isalnum())
        if not limpio:
            continue

        for i in range(len(limpio) - 3):
            ventana = limpio[i:i + 4]
            letras = "".join(DIGITO_A_LETRA.get(c, c) for c in ventana)
            if letras.isalpha() and letras[3] in {"U", "J", "Z"}:
                prefijos[letras].add(idx)

        for i in range(len(limpio) - 6):
            ventana = limpio[i:i + 7]
            digitos = "".join(LETRA_A_DIGITO.get(c, c) for c in ventana)
            if digitos.isdigit():
                cuerpos[digitos].add(idx)

    if not prefijos or not cuerpos:
        return None

    # Acota la combinatoria: si hay demasiados candidatos, prioriza los que
    # aparecen en más textos distintos antes de truncar.
    if len(prefijos) > _MAX_PREFIJOS:
        prefijos = dict(
            sorted(prefijos.items(), key=lambda kv: -len(kv[1]))[:_MAX_PREFIJOS]
        )
    if len(cuerpos) > _MAX_CUERPOS:
        cuerpos = dict(
            sorted(cuerpos.items(), key=lambda kv: -len(kv[1]))[:_MAX_CUERPOS]
        )

    soportes = {}
    for prefijo, idx_prefijo in prefijos.items():
        for cuerpo, idx_cuerpo in cuerpos.items():
            codigo = prefijo + cuerpo
            if es_contenedor_valido(codigo):
                soportes[codigo] = len(idx_prefijo) + len(idx_cuerpo)

    if not soportes:
        return None

    max_soporte = max(soportes.values())
    ganadores = [c for c, s in soportes.items() if s == max_soporte]

    if len(ganadores) == 1:
        return ganadores[0]
    return None


def candidatos_de_regiones(regions, ocr_text=""):
    """
    Genera el set de códigos de contenedor válidos detectados en una
    detección OCR. `regions` es la lista [texto, conf, bbox] de esa
    detección; `ocr_text` es el texto "mejor" asociado (puede repetirse
    con alguna región, de ahí que el resultado sea un set: cada detección
    vota como máximo una vez por código).
    """
    candidatos = set()

    for texto in _textos_fuente(regions, ocr_text):
        for ventana in ventanas_11(texto):
            if es_contenedor_valido(ventana):
                candidatos.add(ventana)
                continue
            corregido = corregir_posicional(ventana)
            if corregido and es_contenedor_valido(corregido):
                candidatos.add(corregido)

    return candidatos
