import logging
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


@shared_task
def aggregate_ocr_results(event_id):
    from collections import Counter

    from aduana.models import ContainerDetection, ContainerEvent

    try:
        event = ContainerEvent.objects.get(id=event_id)
    except ContainerEvent.DoesNotExist:
        logger.warning("aggregate_ocr_results: event %s not found", event_id)
        return

    detections = ContainerDetection.objects.filter(
        event=event, class_id=3, ocr_processed=True
    ).exclude(ocr_text="")

    if detections.count() < 2:
        return

    texts = []
    for d in detections:
        if d.ocr_text and d.ocr_confidence and d.ocr_confidence > 0.6:
            texts.append(d.ocr_text)
        for region in (d.ocr_texts or []):
            if region[1] >= 0.6:
                texts.append(region[0])

    if not texts:
        return

    counter = Counter(texts)
    most_common = counter.most_common(1)[0][0]

    if event.container_code != most_common:
        event.container_code = most_common
        event.save(update_fields=["container_code"])
        logger.info("Event %s OCR consensus: '%s' (from %d readings)", event_id, most_common, len(texts))


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
