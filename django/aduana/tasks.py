import logging
import re
from collections import Counter
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)

GAP_CLUSTER_THRESHOLD = 2.0
COLOR_SPLIT_THRESHOLD = 0.25
COLOR_MERGE_THRESHOLD = 0.20
MERGE_WINDOW = 30
MIN_CLUSTER_SIZE = 3


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

    try:
        result = _run_paddle_ocr(detection.crop.path)
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


def _run_paddle_ocr(image_path):
    try:
        result = _run_ocr_vl(image_path)
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


def _run_ocr_vl(image_path):
    try:
        import requests
    except ImportError:
        return None

    OCR_VL_URL = "http://ocr-vl:5002"

    try:
        with open(image_path, "rb") as f:
            # Try OCR mode first
            resp = requests.post(f"{OCR_VL_URL}/ocr", files={"file": f}, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        raw_text = data.get("text", "").strip()

        # If OCR mode returned nothing, try spotting mode (for vertical text)
        if not raw_text:
            with open(image_path, "rb") as f:
                resp2 = requests.post(f"{OCR_VL_URL}/spotting", files={"file": f}, timeout=10)
            if resp2.status_code == 200:
                data2 = resp2.json()
                raw_text = data2.get("text", "").strip()

        if not raw_text:
            return None

        lines = [line.strip() for line in raw_text.split("\n") if line.strip()]
        regions = [[line, 0.85, []] for line in lines]

        best_text = lines[0] if lines else raw_text
        return {
            "text": best_text,
            "confidence": 0.85,
            "regions": regions,
        }
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

    if detections.count() < 2:
        return

    candidates = []

    for d in detections:
        regions = d.ocr_texts or []
        if not regions:
            continue

        regions_filtered = [r for r in regions if len(r) >= 2 and r[1] >= 0.6]
        if not regions_filtered:
            continue

        has_bbox = any(len(r) >= 3 and len(r[2]) > 0 for r in regions_filtered)

        if has_bbox:
            regions_with_bbox = [r for r in regions_filtered if len(r) >= 3 and len(r[2]) > 0]
            regions_with_bbox.sort(key=lambda r: r[2][0][0])
            ordered = regions_with_bbox
        else:
            ordered = regions_filtered

        full_text = "".join(r[0].upper() for r in ordered)

        found = re.findall(r"[A-Z]{4}\d{7}", full_text)
        for code in found:
            if es_contenedor_valido(code):
                candidates.append(code)

        if d.ocr_text and d.ocr_confidence and d.ocr_confidence >= 0.6:
            full_main = d.ocr_text.upper()
            found_main = re.findall(r"[A-Z]{4}\d{7}", full_main)
            for code in found_main:
                if es_contenedor_valido(code):
                    candidates.append(code)

    if not candidates:
        return

    counter = Counter(candidates)
    most_common = counter.most_common(1)[0][0]

    if event.container_code != most_common:
        event.container_code = most_common
        event.save(update_fields=["container_code"])
        logger.info(
            "Event %s OCR consensus: '%s' (from %d candidates in %d detections)",
            event_id, most_common, len(candidates), detections.count(),
        )


def es_contenedor_valido(contenedor):
    if not isinstance(contenedor, str):
        return False

    limpio = "".join(c.upper() for c in contenedor if c.isalnum())

    if len(limpio) != 11:
        return False

    if not re.match(r"^[A-Z]{4}\d{7}$", limpio):
        return False

    if limpio[3] not in {"U", "J", "Z"}:
        return False

    valores = {}
    n = 10
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if n % 11 == 0:
            n += 1
        valores[c] = n
        n += 1

    total = 0
    for i in range(10):
        char = limpio[i]
        valor = valores[char] if char.isalpha() else int(char)
        total += valor * (2 ** i)

    checksum = total % 11
    if checksum == 10:
        checksum = 0

    return checksum == int(limpio[10])


@shared_task
def close_stale_events():
    from aduana.models import ContainerDetection, ContainerEvent

    threshold = timezone.now() - timedelta(seconds=15)

    open_events = ContainerEvent.objects.filter(
        seal_status="processing", timestamp_end__isnull=True
    )

    for event in open_events:
        last_detection = (
            ContainerDetection.objects.filter(event=event)
            .order_by("-timestamp")
            .first()
        )

        if last_detection is None:
            continue

        if last_detection.timestamp < threshold:
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

    seal_detections = detections.filter(class_id__in=[0, 2])

    con_sello_count = seal_detections.filter(class_id=0).count()
    sin_sello_count = seal_detections.filter(class_id=2).count()

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

    if event.container_code:
        aggregate_ocr_results.delay(event.id)


def _find_temporal_clusters(detections):
    dets = list(detections.order_by("timestamp").values("id", "timestamp", "dominant_color_h", "dominant_color_s", "dominant_color_v"))
    if len(dets) < 2:
        return []

    clusters = []
    current_cluster = [dets[0]]
    for i in range(1, len(dets)):
        gap = (dets[i]["timestamp"] - dets[i - 1]["timestamp"]).total_seconds()
        if gap > GAP_CLUSTER_THRESHOLD:
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
        logger.info(
            "Split: created event %s from event %s (%d detections)",
            new_event.id, event.id, len(cluster_ids),
        )


def _try_merge_event(event):
    from aduana.models import ContainerEvent, ContainerDetection

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

    evt_color = _get_event_avg_color(event)
    prev_color = _get_event_avg_color(prev)

    if evt_color is None or prev_color is None:
        return False

    if _hsv_distance(evt_color, prev_color) > COLOR_MERGE_THRESHOLD:
        return False

    ContainerDetection.objects.filter(event=event).update(event=prev)
    prev.timestamp_end = max(
        prev.timestamp_end or timezone.now(),
        event.timestamp_end or timezone.now(),
    )
    prev.save(update_fields=["timestamp_end"])
    event_id_old = event.id
    event.delete()

    logger.info("Merge: event %s merged into event %s (gap=%.1fs)", event_id_old, prev.id, gap)

    if prev.container_code:
        aggregate_ocr_results.delay(prev.id)

    return True


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
