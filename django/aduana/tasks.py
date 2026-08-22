import logging
import re
import time
from collections import Counter
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)

OCR_VL_URL = "http://ocr-vl:5002"
_ocr_vl_down_logged = False
_ocr_vl_health = {"ok": None, "ts": 0.0}

GAP_CLUSTER_THRESHOLD = 3.0
COLOR_SPLIT_THRESHOLD = 0.25
COLOR_MERGE_THRESHOLD = 0.20
MERGE_WINDOW = 30
MIN_CLUSTER_SIZE = 3
GAP_CROSS_SOURCE = 5.0
OCR_EVENT_MAX_CROPS = 12
OCR_EVENT_MAX_PER_OBJ = 3


def check_ocr_vl_health(ttl=30.0):
    global _ocr_vl_down_logged
    now = time.monotonic()
    if _ocr_vl_health["ts"] and now - _ocr_vl_health["ts"] < ttl:
        return _ocr_vl_health["ok"]

    ok = False
    try:
        import requests
        resp = requests.get(f"{OCR_VL_URL}/health", timeout=3)
        ok = resp.status_code == 200
    except Exception:
        ok = False

    _ocr_vl_health["ok"] = ok
    _ocr_vl_health["ts"] = now

    if ok:
        if _ocr_vl_down_logged:
            logger.critical("*** ALARMA RESUELTA: OCR-VL vuelve a responder ***")
            _ocr_vl_down_logged = False
    elif not _ocr_vl_down_logged:
        logger.critical("*** ALARMA: OCR-VL (%s) no responde. El OCR de contenedores NO funcionara. ***",
                        OCR_VL_URL)
        _ocr_vl_down_logged = True
    return ok


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

    if detection.class_id != 3 or detection.ocr_processed:
        return

    # Only the best crop per (event, object_id) runs OCR — the rest are near-duplicates.
    if detection.event_id:
        siblings = ContainerDetection.objects.filter(
            event_id=detection.event_id,
            object_id=detection.object_id,
            class_id=3,
        )
        best = siblings.order_by("-confidence", "-id").first()
        if best and best.id != detection.id:
            detection.ocr_processed = True
            detection.save(update_fields=["ocr_processed"])
            return
        # If a sibling already produced text, no need to OCR again
        if siblings.exclude(id=detection.id).filter(
            ocr_processed=True, ocr_text__gt=""
        ).exists():
            detection.ocr_processed = True
            detection.save(update_fields=["ocr_processed"])
            return

    if not check_ocr_vl_health():
        return

    try:
        result = _run_paddle_ocr(detection.crop.path)
    except Exception as e:
        logger.critical("*** ALARMA: OCR-VL error irrecuperable para deteccion %s: %s", detection_id, e)
        detection.ocr_processed = True
        detection.save(update_fields=["ocr_processed"])
        return

    if result:
        detection.ocr_text = result["text"][:64]
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
        return _run_ocr_vl(image_path)
    except Exception as e:
        logger.error("OCR-VL failed: %s", e)
        return None


def _ocr_vl_call(image_path, endpoint):
    import requests
    with open(image_path, "rb") as f:
        resp = requests.post(f"{OCR_VL_URL}/{endpoint}", files={"file": f}, timeout=30)
    if resp.status_code != 200:
        return ""
    text = resp.json().get("text", "")
    # Spotting output appends location tokens like <|LOC_81|>
    return re.sub(r"<\|[A-Z]+_\d+\|>", "", text).strip()


def _run_ocr_vl(image_path):
    try:
        from PIL import Image
        img = Image.open(image_path)
        tall = img.size[1] > img.size[0] * 1.2  # vertical text reads better via spotting
    except Exception:
        tall = False

    order = ("spotting", "ocr") if tall else ("ocr", "spotting")
    try:
        for endpoint in order:
            raw_text = _ocr_vl_call(image_path, endpoint)
            if raw_text:
                break
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
def ocr_event(event_id):
    """OCR the best class-3 crops of an event (called once at event close).

    Picks up to OCR_EVENT_MAX_CROPS crops, preferring high confidence and
    distinct objects, then aggregates the candidates.
    """
    from aduana.models import ContainerDetection, ContainerEvent

    try:
        event = ContainerEvent.objects.get(id=event_id)
    except ContainerEvent.DoesNotExist:
        return

    dets = list(
        ContainerDetection.objects.filter(event=event, class_id=3)
        .order_by("-confidence")
    )
    if not dets:
        return

    # Prefer distinct objects: at most N crops per object_id
    picked = []
    per_obj = Counter()
    for d in dets:
        if per_obj[d.object_id] >= OCR_EVENT_MAX_PER_OBJ:
            continue
        picked.append(d)
        per_obj[d.object_id] += 1
        if len(picked) >= OCR_EVENT_MAX_CROPS:
            break

    check_ocr_vl_health()  # alarm logging only; never gates the OCR pass

    for d in picked:
        if d.ocr_processed and d.ocr_text:
            continue
        try:
            result = _run_paddle_ocr(d.crop.path)
        except Exception as e:
            logger.error("ocr_event %s det %s: %s", event_id, d.id, e)
            result = None
        if result:
            d.ocr_text = result["text"][:64]
            d.ocr_confidence = result["confidence"]
            d.ocr_texts = result["regions"]
            d.save(update_fields=["ocr_text", "ocr_confidence", "ocr_texts"])
            logger.info("ocr_event %s det %s: %r", event_id, d.id, result["text"])
        d.ocr_processed = True
        d.save(update_fields=["ocr_processed"])

    aggregate_ocr_results(event_id)

    # Second pass on remaining crops if no code was found
    event.refresh_from_db()
    if not event.container_code:
        rest = [d for d in dets if d not in picked and not (d.ocr_processed and d.ocr_text)]
        for d in rest[:OCR_EVENT_MAX_CROPS]:
            try:
                result = _run_paddle_ocr(d.crop.path)
            except Exception:
                result = None
            if result:
                d.ocr_text = result["text"][:64]
                d.ocr_confidence = result["confidence"]
                d.ocr_texts = result["regions"]
                d.save(update_fields=["ocr_text", "ocr_confidence", "ocr_texts"])
                logger.info("ocr_event %s pass2 det %s: %r", event_id, d.id, result["text"])
            d.ocr_processed = True
            d.save(update_fields=["ocr_processed"])
        if rest:
            aggregate_ocr_results(event_id)


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

    if not detections.exists():
        return

    # Vote: one vote per detection per code (best tier wins inside a detection).
    # code -> [points, distinct raw reads, strict reads, detection ids]
    TIER_PTS = {"strict": 2, "repaired": 1, "raw": 0}
    TIER_RANK = {"strict": 2, "repaired": 1, "raw": 0}
    votes = {}
    for d in detections:
        per_code = {}
        for code, tier, rawseg in _extract_codes(d):
            if code not in per_code or TIER_RANK[tier] > TIER_RANK[per_code[code][0]]:
                per_code[code] = (tier, rawseg)
        for code, (tier, rawseg) in per_code.items():
            ent = votes.setdefault(code, [0, set(), 0, set()])
            ent[0] += TIER_PTS[tier]
            ent[1].add(rawseg)
            ent[3].add(d.id)
            if tier == "strict":
                ent[2] += 1

    if not votes:
        return

    # Raw-only codes backed by >= 3 distinct detections get a bonus
    # (a physically painted label can have an invalid check digit).
    for code, ent in votes.items():
        if ent[2] == 0 and ent[0] == 0 and len(ent[3]) >= 3:
            ent[0] += 2

    # Winner: most points; ties -> more distinct raw reads; then more strict reads
    most_common = max(votes.items(), key=lambda kv: (kv[1][0], len(kv[1][1]), kv[1][2]))[0]
    n_strict = sum(v[2] for v in votes.values())

    if event.container_code != most_common:
        event.container_code = most_common
        event.save(update_fields=["container_code"])
        logger.info(
            "Event %s OCR consensus: '%s' (codes=%d strict_reads=%d, %d detections)",
            event_id, most_common, len(votes), n_strict, detections.count(),
        )


def _compute_check_digit(code10):
    """ISO 6346 check digit from the first 10 chars (4 letters + 6 digits)."""
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

    return _compute_check_digit(limpio[:10]) == int(limpio[10])


def _es_formato_valido(code):
    limpio = re.sub(r"\s+", "", code.upper())
    if len(limpio) != 11:
        return False
    if not re.match(r"^[A-Z]{4}\d{7}$", limpio):
        return False
    if limpio[3] not in {"U", "J", "Z"}:
        return False
    return True


# Digit<->letter confusion sets for OCR repair (position-aware)
_L2D = {"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "Z": "2",
        "S": "5", "G": "6", "B": "8"}
_D2L = {"0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "8": "B"}


def _normalize_positions(seg):
    """Fix digit<->letter confusion at known positions (4 letters + 7 digits)."""
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


def _to_valid_code(seg):
    """Convert a raw alnum segment into an ISO 6346 code when possible.

    Returns (code, strict): strict=True only if the code was read exactly
    as a checksum-valid string. Repairs:
      - digit/letter confusion at known positions (O<->0, I<->1, ...)
      - missing last digit (4 letters + 6 digits) -> compute check digit
      - wrong last digit -> recompute check digit (checksum fails but the
        first 10 chars are usually read correctly by the VL model)
    """
    seg, normalized = _normalize_positions(seg)
    if not re.match(r"^[A-Z]{4}\d{6,7}$", seg):
        return None, False
    if seg[3] not in "UJZ":
        # Category letter (U/J/Z) is often misread (U<->L/I etc). Try U/J/Z,
        # checksum still gates the result (only applies to full 11-char reads).
        if len(seg) == 11:
            for cat in "UJZ":
                cand = seg[:3] + cat + seg[4:]
                if es_contenedor_valido(cand):
                    return cand, False
        return None, False

    if len(seg) == 11:
        if not normalized and es_contenedor_valido(seg):
            return seg, True
        # Trust the first 10 chars, recompute the check digit
        return seg[:10] + str(_compute_check_digit(seg[:10])), False

    # 4 letters + 6 digits: check digit missing -> compute it
    return seg + str(_compute_check_digit(seg)), False


def _extract_codes(detection):
    """Return list of (code, tier, raw_segment) candidates from one detection.

    tier: strict = checksum-valid as read; repaired = fixed via normalization,
          check-digit recovery, or reversed read; raw = format-valid as-is
          (physical label may have a bad check digit).
    """
    out = []
    texts = []
    if detection.ocr_text:
        texts.append(detection.ocr_text)
    for r in (detection.ocr_texts or []):
        if len(r) >= 2 and r[1] >= 0.6:
            texts.append(r[0])
    texts = list(dict.fromkeys(texts))  # dedupe identical lines

    for t in texts:
        clean = re.sub(r"\s+", "", t.upper())
        for m in re.finditer(r"[A-Z0-9]{4}[0-9A-Z]{6,7}", clean):
            seg = m.group(0)
            code, is_strict = _to_valid_code(seg)
            if code:
                out.append((code, "strict" if is_strict else "repaired", seg))
        # Reversed reads: "389111 TEMU 22G1" (serial before owner code)
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


def _fuzzy_consensus(codes, min_votes=2, max_distance=2):
    if not codes:
        return None

    counter = dict()
    for c in codes:
        code = re.sub(r"\s+", "", c.upper())
        if _es_formato_valido(code):
            counter[code] = counter.get(code, 0) + 1

    if not counter:
        return None

    unique = list(counter.keys())
    neighbors = {c: counter[c] for c in unique}

    for i in range(len(unique)):
        for j in range(i + 1, len(unique)):
            dist = _levenshtein(unique[i], unique[j])
            if dist <= max_distance:
                neighbors[unique[i]] += counter[unique[j]]
                neighbors[unique[j]] += counter[unique[i]]

    if not neighbors:
        return None

    best = max(neighbors, key=lambda c: (neighbors[c], counter[c]))
    if neighbors[best] < min_votes:
        return None

    return best


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

    ocr_event.delay(event.id)
    analyze_seals.delay(event.id)
    capture_event_frames.delay(event.id)


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
        logger.info(
            "Split: created event %s from event %s (%d detections)",
            new_event.id, event.id, len(cluster_ids),
        )


def _try_merge_event(event):
    from django.db.models import Q
    from aduana.models import ContainerEvent, ContainerDetection

    # Candidates: events that overlap in time (or are still open) or closed
    # within MERGE_WINDOW before this event started. Both cameras watch the
    # same single-lane zone, so events whose time ranges overlap are
    # necessarily the same physical truck passage — merge unconditionally.
    event_start = event.timestamp_start
    event_end = event.timestamp_end or timezone.now()
    win_start = event_start - timedelta(seconds=MERGE_WINDOW)

    cands = (
        ContainerEvent.objects
        .filter(seal_status__in=["processing", "con_sello", "sin_sello", "indeterminado"],
                timestamp_start__lte=event_end)  # never merge into future events
        .filter(Q(timestamp_end__isnull=True) | Q(timestamp_end__gte=win_start))
        .exclude(id=event.id)
        .order_by("-timestamp_start")
    )

    prev = None
    consecutive = None
    for cand in cands[:10]:
        cand_end = cand.timestamp_end
        if cand_end is None:
            overlaps = cand.timestamp_start <= event_end
        else:
            overlaps = (cand.timestamp_start <= event_end
                        and cand_end >= event_start)
        if overlaps:
            prev = cand
            break
        if consecutive is None and cand_end is not None:
            consecutive = cand  # latest non-overlapping closed event in window

    overlap_merge = prev is not None
    if prev is None:
        # No temporal overlap: consecutive events may still be the same truck,
        # but could also be two different trucks — keep the color check as a
        # safeguard for that ambiguous case.
        prev = consecutive
        if prev is None:
            return False
        evt_color = _get_event_avg_color(event)
        prev_color = _get_event_avg_color(prev)
        if evt_color is None or prev_color is None:
            return False
        if _hsv_distance(evt_color, prev_color) > COLOR_MERGE_THRESHOLD:
            return False

    was_open = prev.timestamp_end is None
    update_fields = ["timestamp_start"]
    ContainerDetection.objects.filter(event=event).update(event=prev)
    prev.timestamp_start = min(prev.timestamp_start, event_start)
    if not was_open:
        prev.timestamp_end = max(prev.timestamp_end, event_end)
        update_fields.append("timestamp_end")
    prev.save(update_fields=update_fields)
    event_id_old = event.id
    event.delete()

    logger.info("Merge: event %s merged into event %s (overlap=%s)",
                event_id_old, prev.id, overlap_merge)

    if was_open:
        # The surviving event is still accumulating detections; it will run
        # seal/OCR analysis when it closes.
        return True

    if prev.container_code:
        aggregate_ocr_results.delay(prev.id)
    else:
        ocr_event.delay(prev.id)

    return True

    if prev.container_code:
        aggregate_ocr_results.delay(prev.id)
    else:
        ocr_event.delay(prev.id)

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


# ---------------------------------------------------------------------------
# Seal grid analysis (door positions 1-8)
# ---------------------------------------------------------------------------

EVENT_FRAME_FPS = 20.0        # test videos are 20 fps (production too)
PIPELINE_CONFIG = "/opt/computer_vision/config/config_aduana_test.yml"


def _source_video_paths():
    """Map source index -> video file path from the pipeline config source-list."""
    try:
        with open(PIPELINE_CONFIG) as f:
            text = f.read()
        m = re.search(r'list:\s*"([^"]+)"', text)
        if not m:
            return {}
        uris = m.group(1).split(";")
        return {i: u.replace("file://", "") for i, u in enumerate(uris)}
    except OSError:
        return {}


@shared_task
def capture_event_frames(event_id):
    """Extract the best full frame per camera for an event (test/MP4 flow).

    Best frame per source = the frame with the most seal detections;
    ties broken by most total detections, then max confidence sum.
    Production (RTSP) uses pipeline-side capture instead (no file to seek).
    """
    import subprocess
    from collections import defaultdict
    from django.core.files.base import ContentFile
    from aduana.models import ContainerEvent

    try:
        event = ContainerEvent.objects.get(id=event_id)
    except ContainerEvent.DoesNotExist:
        return

    videos = _source_video_paths()
    if not videos:
        return

    changed = False
    for sid, vpath in videos.items():
        dets = event.detections.filter(source_id=sid)
        if not dets.exists():
            continue

        per_frame = defaultdict(lambda: [0, 0, 0.0])  # seals, total, conf sum
        for d in dets.values("frame_num", "class_id", "confidence"):
            fn = d["frame_num"]
            per_frame[fn][1] += 1
            per_frame[fn][2] += d["confidence"]
            if d["class_id"] in (0, 1):
                per_frame[fn][0] += 1
        if not per_frame:
            continue

        best_fn = max(per_frame.items(), key=lambda kv: (kv[1][0], kv[1][1], kv[1][2]))[0]
        seconds = best_fn / EVENT_FRAME_FPS
        try:
            out = subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{seconds:.3f}", "-i", vpath,
                 "-vf", "scale=1280:-2", "-frames:v", "1", "-q:v", "2",
                 "-f", "image2", "-"],
                capture_output=True, timeout=120,
            )
            if out.returncode == 0 and out.stdout:
                getattr(event, f"frame_src{sid}").save(
                    f"event_{event_id}_src{sid}.jpg", ContentFile(out.stdout), save=False
                )
                changed = True
                logger.info("Event %s src%d: frame %d (t=%.1fs) captured",
                            event_id, sid, best_fn, seconds)
        except Exception as e:
            logger.warning("capture_event_frames %s src%d: %s", event_id, sid, e)

    if changed:
        event.save(update_fields=["frame_src0", "frame_src1"])


SEAL_CLUSTER_DIST = 0.03      # max normalized distance to merge track fragments
SEAL_ROW_GAP = 0.035          # min y gap between door rows
SEAL_COL_GAP_FACTOR = 1.8     # column gap = factor x median column spacing
SEAL_ROW_LAYOUT = [2, 2, 4]   # columns per row: top 1-2, middle 3-4, bottom 5-8


def _seal_grid_for_source(event, sid):
    """Build door-position map for one camera using velocity-compensated
    canonical positions. Returns dict {pos: {...}} or None."""
    import numpy as np

    dets = list(
        event.detections.filter(source_id=sid, class_id__in=[0, 1])
        .order_by("timestamp")
        .values("id", "object_id", "class_id", "confidence", "timestamp",
                "bbox_left", "bbox_top", "bbox_width", "bbox_height", "crop")
    )
    if not dets:
        return None

    t0 = dets[0]["timestamp"].timestamp()
    tracks = {}
    for d in dets:
        cx = d["bbox_left"] + d["bbox_width"] / 2
        cy = d["bbox_top"] + d["bbox_height"] / 2
        tracks.setdefault(d["object_id"], []).append(
            (d["timestamp"].timestamp() - t0, cx, cy, d["class_id"], d["confidence"],
             d["id"], d["crop"])
        )

    # Shared velocity: the door is rigid, all seals translate together.
    vels = []
    for oid, pts in tracks.items():
        if len(pts) < 3:
            continue
        ts = np.array([p[0] for p in pts])
        xs = np.array([p[1] for p in pts])
        ys = np.array([p[2] for p in pts])
        vels.append((np.polyfit(ts, xs, 1)[0], np.polyfit(ts, ys, 1)[0]))
    vx = float(np.median([v[0] for v in vels])) if vels else 0.0
    vy = float(np.median([v[1] for v in vels])) if vels else 0.0

    t_ref = (dets[-1]["timestamp"].timestamp() - t0) / 2

    # Canonical position per track: collapse each trajectory to t_ref.
    canons = []
    for oid, pts in tracks.items():
        ts = np.array([p[0] for p in pts])
        xs = np.array([p[1] for p in pts])
        ys = np.array([p[2] for p in pts])
        cx = float(np.mean(xs + vx * (t_ref - ts)))
        cy = float(np.mean(ys + vy * (t_ref - ts)))
        cls = Counter(p[3] for p in pts).most_common(1)[0][0]
        conf = float(np.mean([p[4] for p in pts]))
        # Best crop of this track = highest confidence detection with an image
        best = max(pts, key=lambda p: p[4])
        canons.append({"x": cx, "y": cy, "cls": cls, "conf": conf, "n": len(pts),
                       "crop": best[6]})

    if not canons:
        return None

    # Cluster fragments of the same physical seal (biggest tracks first).
    clusters = []
    for c in sorted(canons, key=lambda c: -c["n"]):
        placed = False
        for cl in clusters:
            if (cl["x"] - c["x"]) ** 2 + (cl["y"] - c["y"]) ** 2 < SEAL_CLUSTER_DIST ** 2:
                tot = cl["n"] + c["n"]
                cl["x"] = (cl["x"] * cl["n"] + c["x"] * c["n"]) / tot
                cl["y"] = (cl["y"] * cl["n"] + c["y"] * c["n"]) / tot
                cl["conf"] = (cl["conf"] * cl["n"] + c["conf"] * c["n"]) / tot
                cl["votes"][c["cls"]] = cl["votes"].get(c["cls"], 0) + c["n"]
                cl["n"] = tot
                if c["conf"] > cl["conf"]:
                    cl["crop"] = c["crop"]  # keep the sharpest image
                placed = True
                break
        if not placed:
            clusters.append({"x": c["x"], "y": c["y"], "conf": c["conf"],
                             "n": c["n"], "votes": {c["cls"]: c["n"]},
                             "crop": c["crop"]})

    # Split into row bands (top 1-2 / middle 3-4 / bottom 5-8) by y gaps.
    clusters.sort(key=lambda c: c["y"])
    bands = [[clusters[0]]]
    for i in range(1, len(clusters)):
        if clusters[i]["y"] - clusters[i - 1]["y"] > SEAL_ROW_GAP:
            bands.append([])
        bands[-1].append(clusters[i])

    # Merge down to at most 3 bands (smallest band merges into nearest by y).
    while len(bands) > 3:
        sizes = [sum(c["n"] for c in b) for b in bands]
        centers = [float(np.mean([c["y"] for c in b])) for b in bands]
        i = sizes.index(min(sizes))
        if i == 0:
            tgt = 1
        elif i == len(bands) - 1:
            tgt = i - 1
        else:
            tgt = (i - 1 if abs(centers[i - 1] - centers[i]) < abs(centers[i + 1] - centers[i])
                   else i + 1)
        bands[tgt].extend(bands[i])
        del bands[i]

    # Map bands to layout rows: [top(1-2), middle(3-4), bottom(5-8)]
    if len(bands) == 3:
        row_idx = [0, 1, 2]
    elif len(bands) == 2:
        c0 = float(np.mean([c["y"] for c in bands[0]]))
        c1 = float(np.mean([c["y"] for c in bands[1]]))
        row_idx = [0, 2] if abs(c1 - c0) > 2 * SEAL_ROW_GAP else [0, 1]
    else:
        row_idx = [2]  # single band: bottom row (the 4 seal points)

    grid = {}
    for band_i, band in enumerate(bands):
        r = row_idx[band_i]
        ncols = SEAL_ROW_LAYOUT[r]
        base = 1 + sum(SEAL_ROW_LAYOUT[:r])  # first position of the row: 1, 3, 5

        band = sorted(band, key=lambda c: c["x"])
        xs = [c["x"] for c in band]
        spacings = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
        med_sp = float(np.median(spacings)) if spacings else 0.0
        col = 0
        for i, c in enumerate(band):
            if i == 0:
                col = 1
            else:
                gap = xs[i] - xs[i - 1]
                if med_sp > 0 and gap > SEAL_COL_GAP_FACTOR * med_sp:
                    col += max(2, int(round(gap / med_sp)))
                else:
                    col += 1
            col = min(col, ncols)
            pos = base + col - 1
            status = "con_sello" if max(c["votes"], key=c["votes"].get) == 0 else "sin_sello"
            cell = {
                "status": status,
                "conf": round(c["conf"], 3),
                "n": c["n"],
                "src": sid,
                "crop": c.get("crop", ""),
            }
            # Keep the cluster with more detections on column collisions
            if str(pos) not in grid or grid[str(pos)]["n"] < c["n"]:
                grid[str(pos)] = cell
    return grid


def _merge_seal_grids(g0, g1):
    """Union of both cameras' grids; same physical position in both.
    Prefer the reading with more detections."""
    if not g0 and not g1:
        return {}
    if not g0:
        return g1
    if not g1:
        return g0
    merged = dict(g0)
    for pos, info in g1.items():
        if pos not in merged or info.get("n", 0) > merged[pos].get("n", 0):
            merged[pos] = info
    return merged


@shared_task
def analyze_seals(event_id):
    """Assign door positions 1-8 to seal detections of an event.

    The door layout is fixed: top row 1-4, bottom row 5-8 (seen from behind,
    same orientation in both cameras). Computed with velocity-compensated
    canonical positions so truck movement and tracker fragmentation do not
    affect the assignment.
    """
    from aduana.models import ContainerEvent

    try:
        event = ContainerEvent.objects.get(id=event_id)
    except ContainerEvent.DoesNotExist:
        return

    grids = {}
    for sid in (0, 1):
        g = _seal_grid_for_source(event, sid)
        if g:
            grids[sid] = g

    merged = _merge_seal_grids(grids.get(0), grids.get(1))
    for pos in range(1, 9):
        merged.setdefault(str(pos), {"status": "sin detección"})

    event.seal_grid = merged
    event.save(update_fields=["seal_grid"])
    found = sum(1 for v in merged.values() if v["status"] != "sin detección")
    logger.info("Event %s seal grid: %d/8 positions detected", event_id, found)
