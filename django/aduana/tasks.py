import logging
import re
from collections import Counter
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


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

    OCR_VL_URL = "http://ocr-vl:5002/ocr"

    try:
        with open(image_path, "rb") as f:
            resp = requests.post(OCR_VL_URL, files={"file": f}, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        raw_text = data.get("text", "").strip()
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
    from collections import Counter

    from aduana.models import ContainerDetection

    detections = ContainerDetection.objects.filter(event=event)
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
