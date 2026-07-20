import logging
from collections import Counter, defaultdict
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from aduana.ocr_codes import (  # noqa: F401 (re-exportado)
    candidatos_con_origen,
    candidatos_de_regiones,
    consenso_parcial,
    es_contenedor_valido,
    soporte_parcial,
    texto_tiene_codigo_valido,
)

logger = logging.getLogger(__name__)

GAP_CLUSTER_THRESHOLD = 3.0
COLOR_SPLIT_THRESHOLD = 0.25
COLOR_MERGE_THRESHOLD = 0.20
MERGE_WINDOW = 30
MIN_CLUSTER_SIZE = 3
GAP_CROSS_SOURCE = 5.0


def _hsv_distance(c1, c2):
    dh = min(abs(c1[0] - c2[0]), 1.0 - abs(c1[0] - c2[0]))
    ds = abs(c1[1] - c2[1])
    dv = abs(c1[2] - c2[2])
    return ((dh * 1.5) ** 2 + ds ** 2 + (dv * 0.5) ** 2) ** 0.5


@shared_task
def process_ocr(detection_id):
    from aduana.models import ContainerDetection

    try:
        detection = ContainerDetection.objects.get(id=detection_id)
    except ContainerDetection.DoesNotExist:
        logger.warning("process_ocr: detection %s not found", detection_id)
        return

    if detection.class_id != 3:
        return

    vertical = detection.bbox_height > detection.bbox_width

    try:
        result = _run_paddle_ocr(detection.crop.path, vertical=vertical)
    except Exception as e:
        logger.error("PaddleOCR failed for detection %s: %s", detection_id, e)
        detection.ocr_processed = True
        detection.save(update_fields=["ocr_processed"])
        return

    if result:
        detection.ocr_text = result["text"]
        detection.ocr_confidence = result["confidence"]
        detection.ocr_texts = result["regions"]
        detection.ocr_processed = True
        detection.save(update_fields=["ocr_text", "ocr_confidence", "ocr_texts", "ocr_processed"])
        logger.info(
            "OCR detection %s: '%s' (conf=%.3f, regions=%d)",
            detection_id,
            result["text"],
            result["confidence"],
            len(result["regions"]),
        )

        if detection.event_id:
            aggregate_ocr_results.delay(detection.event_id)
    else:
        detection.ocr_processed = True
        detection.save(update_fields=["ocr_processed"])


def _run_paddle_ocr(image_path, vertical=False):
    try:
        result = _run_ocr_vl(image_path, vertical=vertical)
        if result:
            return result
    except Exception as e:
        logger.warning("OCR-VL failed, falling back to PaddleOCR: %s", e)

    try:
        from paddleocr import PaddleOCR
    except ImportError:
        logger.error("PaddleOCR not installed")
        return None

    ocr = PaddleOCR(lang="en", use_angle_cls=True, show_log=False, det_db_thresh=0.15)
    results = ocr.ocr(image_path)

    if not results or not results[0]:
        return None

    from PIL import Image

    img = Image.open(image_path)
    img_w, img_h = img.size

    regions = []
    best_text = ""
    best_conf = 0.0

    for region in results[0]:
        text = region[1][0].strip()
        conf = float(region[1][1])
        if text and conf >= 0.6:
            bbox = region[0]
            normalized_bbox = [[p[0] / img_w, p[1] / img_h] for p in bbox]
            regions.append([text, conf, normalized_bbox])
            if conf > best_conf:
                best_text = text
                best_conf = conf

    if not regions:
        return None

    return {
        "text": best_text,
        "confidence": best_conf,
        "regions": regions,
    }


def _run_ocr_vl(image_path, vertical=False):
    try:
        import requests
    except ImportError:
        return None

    OCR_VL_URL = "http://ocr-vl:5002"

    # El texto vertical sale truncado/vacío de /ocr con más frecuencia;
    # /spotting funciona mejor para ese caso, así que se prueba primero.
    modes = ["/spotting", "/ocr"] if vertical else ["/ocr", "/spotting"]

    def _call(mode):
        # El file handle no es reutilizable entre requests: se reabre cada vez.
        with open(image_path, "rb") as f:
            resp = requests.post(f"{OCR_VL_URL}{mode}", files={"file": f}, timeout=10)
        if resp.status_code != 200:
            return ""
        data = resp.json()
        return data.get("text", "").strip()

    def _lines(text):
        return [line.strip() for line in text.split("\n") if line.strip()]

    def _merge_unique(base, extra):
        combined = list(base)
        for line in extra:
            if line not in combined:
                combined.append(line)
        return combined

    try:
        first_text = _call(modes[0])
        first_lines = _lines(first_text)

        if first_text and texto_tiene_codigo_valido(first_text):
            return {
                "text": first_lines[0] if first_lines else first_text,
                "confidence": 0.85,
                "regions": [[line, 0.85, []] for line in first_lines],
            }

        second_text = _call(modes[1])
        second_lines = _lines(second_text)

        if second_text and texto_tiene_codigo_valido(second_text):
            regions_lines = _merge_unique(second_lines, first_lines)
            return {
                "text": second_lines[0] if second_lines else second_text,
                "confidence": 0.85,
                "regions": [[line, 0.85, []] for line in regions_lines],
            }

        if first_text or second_text:
            regions_lines = _merge_unique(first_lines, second_lines)
            if not regions_lines:
                return None
            return {
                "text": regions_lines[0],
                "confidence": 0.85,
                "regions": [[line, 0.85, []] for line in regions_lines],
            }

        return None
    except Exception:
        return None


@shared_task
def aggregate_ocr_results(event_id):
    from aduana.models import ContainerDetection, ContainerEvent

    try:
        event = ContainerEvent.objects.get(id=event_id)
    except ContainerEvent.DoesNotExist:
        logger.warning("aggregate_ocr_results: event %s not found", event_id)
        return

    detections = ContainerDetection.objects.filter(
        event=event, class_id=3, ocr_processed=True
    )

    # Basta 1 detección: el checksum ISO 6346 hace muy improbable un falso
    # positivo, así que no exigimos un mínimo de detecciones.
    if not detections.exists():
        return

    # Lista de (code, source_id, es_directo) — 1 voto por código por
    # detección; es_directo=True si el código apareció literalmente (sin
    # corrección posicional) en esa detección. `textos` junta todas las
    # lecturas crudas del evento: se usan para el consenso parcial y como
    # soporte de desempate.
    votes = []
    textos = []
    for d in detections:
        regions = d.ocr_texts or []
        regions_filtered = [r for r in regions if len(r) >= 2 and r[1] >= 0.6]

        codigos = candidatos_con_origen(regions_filtered, d.ocr_text or "")
        for codigo, es_directo in codigos.items():
            votes.append((codigo, d.source_id, es_directo))

        for r in regions_filtered:
            if isinstance(r[0], str) and r[0]:
                textos.append(r[0])
        if d.ocr_text:
            textos.append(d.ocr_text)

    if not votes:
        # Ningún texto individual contiene por sí solo un código completo y
        # válido. Antes de rendirnos, probamos a reconstruirlo combinando
        # fragmentos parciales de distintas lecturas (p.ej. un crop con solo
        # el prefijo de letras y otro con solo los dígitos).
        codigo = consenso_parcial(textos)
        if not codigo:
            return

        if event.container_code != codigo:
            event.container_code = codigo
            event.save(update_fields=["container_code"])
        logger.info(
            "Event %s OCR partial-consensus: '%s' (from %d texts)",
            event_id, codigo, len(textos),
        )
        most_common = codigo
    else:
        counter = Counter(code for code, _, _ in votes)
        max_votes = max(counter.values())
        tied = sorted(code for code, n in counter.items() if n == max_votes)

        if len(tied) == 1:
            most_common = tied[0]
        else:
            # Desempate 1: votos directos (código leído literalmente, sin
            # corrección posicional — evidencia más fuerte).
            direct_by_code = Counter()
            sources_by_code = defaultdict(set)
            for code, source_id, es_directo in votes:
                if code in tied:
                    sources_by_code[code].add(source_id)
                    if es_directo:
                        direct_by_code[code] += 1
            max_direct = max(direct_by_code.get(c, 0) for c in tied)
            tied = sorted(c for c in tied if direct_by_code.get(c, 0) == max_direct)

            # Desempate 2: cantidad de cámaras (source_id) distintas.
            if len(tied) > 1:
                max_sources = max(len(sources_by_code[c]) for c in tied)
                tied = sorted(c for c in tied if len(sources_by_code[c]) == max_sources)

            # Desempate 3: soporte parcial — cuántas lecturas crudas del
            # evento contienen el prefijo o el cuerpo del código (las
            # lecturas parciales tipo "EGSU389024" no votan, pero sí
            # respaldan a un candidato sobre otro).
            if len(tied) > 1:
                soporte = {c: soporte_parcial(c, textos) for c in tied}
                max_soporte = max(soporte.values())
                tied = sorted(c for c in tied if soporte[c] == max_soporte)

            most_common = tied[0]
            if len(tied) > 1:
                logger.warning(
                    "Event %s OCR tie unresolved between %s — picked '%s' (revisar manualmente)",
                    event_id, tied, most_common,
                )

        if event.container_code != most_common:
            event.container_code = most_common
            event.save(update_fields=["container_code"])
            logger.info(
                "Event %s OCR consensus: '%s' (from %d candidates in %d detections)",
                event_id, most_common, len(votes), detections.count(),
            )

    # El voto directo y el consenso parcial ya asignaron (o confirmaron)
    # event.container_code. Como red de seguridad adicional, buscamos otro
    # evento con el MISMO código dentro de la ventana de merge: es el caso
    # de un contenedor partido en 2 eventos consecutivos (uno con código y
    # otro sin él, o ambos con el mismo código detectado por separado).
    #
    # Nota sobre recursión: NO se encola un nuevo aggregate_ocr_results desde
    # este merge (requeue=False). Ambos eventos ya comparten el mismo código
    # confirmado, así que no hay nada nuevo que recalcular; encolar aquí solo
    # generaría una re-ejecución redundante (con CELERY_TASK_ALWAYS_EAGER,
    # anidada dentro de esta misma llamada) sin beneficio. La búsqueda de
    # merge, además, siempre excluye al propio evento y el evento fusionado
    # deja de existir (se borra), así que no hay forma de repetir el mismo
    # match dos veces.
    other = _find_merge_candidate_by_code(event, most_common)
    if other is not None:
        if event.timestamp_start <= other.timestamp_start:
            _merge_into(event, other, requeue=False)
        else:
            _merge_into(other, event, requeue=False)
            return  # `event` fue fusionado dentro de `other` y ya no existe


@shared_task
def close_stale_events():
    from aduana.models import ContainerDetection, ContainerEvent

    threshold = timezone.now() - timedelta(seconds=15)
    seal_threshold = timezone.now() - timedelta(seconds=3)
    roi_exit_threshold = timezone.now() - timedelta(seconds=2)

    open_events = ContainerEvent.objects.filter(
        seal_status="processing", timestamp_end__isnull=True
    )

    for event in open_events:
        detections = ContainerDetection.objects.filter(event=event)
        last_detection = detections.order_by("-timestamp").first()

        if last_detection is None:
            continue

        should_close = False

        if last_detection.timestamp < threshold:
            should_close = True

        if not should_close:
            seal_dets = detections.filter(class_id__in=[0, 1])
            if seal_dets.exists():
                last_seal = seal_dets.order_by("-timestamp").first()
                if last_seal.timestamp < seal_threshold:
                    should_close = True

        if not should_close:
            exit_dets = detections.filter(roi_name="salida")
            if exit_dets.exists():
                last_exit = exit_dets.order_by("-timestamp").first()
                if last_exit.timestamp < roi_exit_threshold:
                    should_close = True

        if should_close:
            _finalize_event(event)


def _finalize_event(event):
    from aduana.models import ContainerDetection

    detections = ContainerDetection.objects.filter(event=event)
    if detections.count() == 0:
        return

    clusters = _find_temporal_clusters(detections)
    if len(clusters) >= 2:
        _split_event(event, clusters)
        detections = ContainerDetection.objects.filter(event=event)
        if detections.count() == 0:
            return

    if _try_merge_event(event):
        return

    seal_detections = detections.filter(class_id__in=[0, 1])

    con_sello_count = seal_detections.filter(class_id=0).count()
    sin_sello_count = seal_detections.filter(class_id=1).count()

    if con_sello_count == 0 and sin_sello_count == 0:
        event.seal_status = "indeterminado"
        event.seal_confidence = 0.0
    elif con_sello_count > sin_sello_count:
        event.seal_status = "con_sello"
        total = con_sello_count + sin_sello_count
        event.seal_confidence = con_sello_count / total if total > 0 else 0.0
    elif sin_sello_count > con_sello_count:
        event.seal_status = "sin_sello"
        total = con_sello_count + sin_sello_count
        event.seal_confidence = sin_sello_count / total if total > 0 else 0.0
    else:
        event.seal_status = "indeterminado"
        event.seal_confidence = 0.5

    event.timestamp_end = timezone.now()
    event.save(update_fields=["seal_status", "seal_confidence", "timestamp_end"])
    logger.info(
        "Event %s finalized: seal=%s (conf=%.2f) con=%d sin=%d",
        event.id,
        event.seal_status,
        event.seal_confidence,
        con_sello_count,
        sin_sello_count,
    )

    aggregate_ocr_results.delay(event.id)


def _find_temporal_clusters(detections):
    dets = list(detections.order_by("timestamp").values(
        "id", "timestamp", "source_id",
        "dominant_color_h", "dominant_color_s", "dominant_color_v",
    ))
    if len(dets) < 2:
        return []

    clusters = []
    current_cluster = [dets[0]]
    for i in range(1, len(dets)):
        gap = (dets[i]["timestamp"] - dets[i - 1]["timestamp"]).total_seconds()
        cross_source = dets[i]["source_id"] != dets[i - 1]["source_id"]
        threshold = GAP_CROSS_SOURCE if cross_source else GAP_CLUSTER_THRESHOLD
        if gap > threshold:
            if len(current_cluster) >= MIN_CLUSTER_SIZE:
                clusters.append([d["id"] for d in current_cluster])
            current_cluster = [dets[i]]
        else:
            current_cluster.append(dets[i])

    if len(current_cluster) >= MIN_CLUSTER_SIZE:
        clusters.append([d["id"] for d in current_cluster])

    if len(clusters) < 2:
        return []

    cluster_colors = []
    for cl in clusters:
        hs = [d["dominant_color_h"] for d in dets if d["id"] in cl and d["dominant_color_h"] is not None]
        ss = [d["dominant_color_s"] for d in dets if d["id"] in cl and d["dominant_color_s"] is not None]
        vs = [d["dominant_color_v"] for d in dets if d["id"] in cl and d["dominant_color_v"] is not None]
        if len(hs) >= 2:
            cluster_colors.append((sum(hs) / len(hs), sum(ss) / len(ss), sum(vs) / len(vs)))
        else:
            cluster_colors.append(None)

    distinct = False
    for i in range(len(clusters)):
        for j in range(i + 1, len(clusters)):
            if cluster_colors[i] and cluster_colors[j]:
                if _hsv_distance(cluster_colors[i], cluster_colors[j]) > COLOR_SPLIT_THRESHOLD:
                    distinct = True
                    break
        if distinct:
            break

    if not distinct:
        return []

    return clusters


def _split_event(event, clusters):
    from aduana.models import ContainerDetection, ContainerEvent

    for i in range(1, len(clusters)):
        cluster_ids = clusters[i]
        if len(cluster_ids) < MIN_CLUSTER_SIZE:
            continue
        dets = ContainerDetection.objects.filter(id__in=cluster_ids).order_by("timestamp")
        first_ts = dets.first().timestamp
        new_event = ContainerEvent.objects.create(
            seal_status="processing",
            timestamp_start=first_ts,
        )
        dets.update(event=new_event)
        aggregate_ocr_results.delay(new_event.id)
        logger.info(
            "Split: created event %s from event %s (%d detections)",
            new_event.id, event.id, len(cluster_ids),
        )


def _try_merge_event(event):
    from aduana.models import ContainerEvent

    prev = (
        ContainerEvent.objects
        .filter(
            timestamp_end__isnull=False,
            timestamp_end__lt=event.timestamp_start,
            seal_status__in=["con_sello", "sin_sello", "indeterminado"],
        )
        .order_by("-timestamp_end")
        .first()
    )

    if prev is None:
        return False

    gap = (event.timestamp_start - prev.timestamp_end).total_seconds()
    if gap > MERGE_WINDOW:
        return False

    if prev.container_code and event.container_code and prev.container_code == event.container_code:
        # Mismo contenedor ya confirmado por código en ambos eventos: el
        # color no importa, se fusionan igual.
        logger.info(
            "Merge: same container_code '%s' between event %s and %s (gap=%.1fs)",
            prev.container_code, event.id, prev.id, gap,
        )
        return _merge_into(prev, event)

    evt_color = _get_event_avg_color(event)
    prev_color = _get_event_avg_color(prev)

    if evt_color is None or prev_color is None:
        return False

    if _hsv_distance(evt_color, prev_color) > COLOR_MERGE_THRESHOLD:
        return False

    return _merge_into(prev, event)


def _merge_into(prev, event, requeue=True):
    """
    Fusiona `event` dentro de `prev`: mueve sus detecciones, extiende
    timestamp_end, borra `event` y (opcionalmente) encola una nueva
    agregación OCR para `prev`. Devuelve True siempre.

    `requeue=False` se usa cuando el llamador ya sabe que no hace falta
    recalcular nada (p.ej. el merge por código en aggregate_ocr_results,
    donde ambos eventos ya comparten el mismo container_code confirmado).
    """
    from aduana.models import ContainerDetection

    ContainerDetection.objects.filter(event=event).update(event=prev)
    prev.timestamp_end = max(
        prev.timestamp_end or timezone.now(),
        event.timestamp_end or timezone.now(),
    )
    prev.save(update_fields=["timestamp_end"])
    event_id_old = event.id
    event.delete()

    logger.info("Merge: event %s merged into event %s", event_id_old, prev.id)

    if requeue:
        aggregate_ocr_results.delay(prev.id)

    return True


def _events_gap(a, b):
    """
    Distancia temporal (segundos) entre los rangos [a.timestamp_start,
    a.timestamp_end] y [b.timestamp_start, b.timestamp_end]. Si un evento
    está abierto (timestamp_end None), se usa su timestamp_start como
    proxy de "fin" (aún no sabemos cuánto más va a durar). Si los rangos
    se superponen, la distancia es 0.
    """
    a_end = a.timestamp_end or a.timestamp_start
    b_end = b.timestamp_end or b.timestamp_start

    if a_end < b.timestamp_start:
        return (b.timestamp_start - a_end).total_seconds()
    if b_end < a.timestamp_start:
        return (a.timestamp_start - b_end).total_seconds()
    return 0.0


def _find_merge_candidate_by_code(event, code):
    """
    Busca, entre los OTROS eventos con el mismo container_code, el más
    cercano temporalmente a `event` dentro de MERGE_WINDOW segundos.

    Solo se considera candidato si ambos eventos están cerrados, o si el
    otro evento está cerrado y `event` sigue abierto (ver spec: no se
    fusiona si `event` está cerrado y el otro sigue abierto/en curso).
    Si hay más de un candidato, se elige el más cercano (más conservador).
    """
    from aduana.models import ContainerEvent

    if not code:
        return None

    candidatos = ContainerEvent.objects.filter(container_code=code).exclude(id=event.id)

    mejor = None
    mejor_gap = None
    for other in candidatos:
        both_closed = event.timestamp_end is not None and other.timestamp_end is not None
        other_closed_event_open = other.timestamp_end is not None and event.timestamp_end is None
        if not (both_closed or other_closed_event_open):
            continue

        gap = _events_gap(event, other)
        if gap >= MERGE_WINDOW:
            continue

        if mejor is None or gap < mejor_gap:
            mejor = other
            mejor_gap = gap

    return mejor


def _get_event_avg_color(event):
    from aduana.models import ContainerDetection

    dets = ContainerDetection.objects.filter(
        event=event, dominant_color_h__isnull=False
    ).values_list("dominant_color_h", "dominant_color_s", "dominant_color_v")

    colors = list(dets)
    if len(colors) < 2:
        return None

    avg_h = sum(c[0] for c in colors) / len(colors)
    avg_s = sum(c[1] for c in colors) / len(colors)
    avg_v = sum(c[2] for c in colors) / len(colors)
    return avg_h, avg_s, avg_v
