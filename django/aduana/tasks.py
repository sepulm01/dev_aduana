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
        detection.ocr_processed = True
        detection.save(update_fields=["ocr_text", "ocr_confidence", "ocr_processed"])
        logger.info(
            "OCR detection %s: '%s' (conf=%.3f)",
            detection_id,
            result["text"],
            result["confidence"],
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

    ocr = PaddleOCR(lang="en", use_angle_cls=False, show_log=False)
    results = ocr.ocr(image_path)

    if not results or not results[0]:
        return None

    best = max(results[0], key=lambda r: r[1][1])
    text = best[1][0].strip()
    confidence = float(best[1][1])

    if not text or confidence < 0.6:
        return None

    return {"text": text, "confidence": confidence}


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

    texts = [
        d.ocr_text
        for d in detections
        if d.ocr_text and d.ocr_confidence and d.ocr_confidence > 0.6
    ]
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
