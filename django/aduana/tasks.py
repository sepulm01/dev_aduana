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

OCR_EVENT_MAX_CROPS = 12
OCR_EVENT_MAX_PER_OBJ = 3
# YOLO confidence below ~0.85 is essentially noise for OCR (measured on
# production data: 0-5% checksum-valid reads vs 14% above 0.85). Events
# without good crops stay codeless rather than getting a phantom code.
OCR_MIN_CROP_CONF = 0.80


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
        ContainerDetection.objects.filter(
            event=event, class_id=3, confidence__gte=OCR_MIN_CROP_CONF)
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
        event=event, class_id=3, ocr_processed=True,
        confidence__gte=OCR_MIN_CROP_CONF,
    )

    if not detections.exists():
        return

    # Vote: one vote per tracked object per code (best tier wins inside a
    # detection, then best tier per object). A tracked object repeats the
    # same reading across frames; counting each frame as an independent vote
    # amplifies repeated OCR errors (identical hallucinations).
    # code -> [points, distinct raw reads, strict reads, object ids]
    TIER_PTS = {"strict": 2, "repaired": 1, "raw": 0}
    TIER_RANK = {"strict": 2, "repaired": 1, "raw": 0}
    per_object = {}
    for d in detections:
        per_object.setdefault(d.object_id, []).append(d)

    votes = {}
    for obj_id, obj_dets in per_object.items():
        best_per_code = {}
        for d in obj_dets:
            per_code = {}
            for code, tier, rawseg in _extract_codes(d):
                if code not in per_code or TIER_RANK[tier] > TIER_RANK[per_code[code][0]]:
                    per_code[code] = (tier, rawseg)
            for code, (tier, rawseg) in per_code.items():
                cur = best_per_code.get(code)
                if cur is None or TIER_RANK[tier] > TIER_RANK[cur[0]]:
                    best_per_code[code] = (tier, rawseg)
        for code, (tier, rawseg) in best_per_code.items():
            ent = votes.setdefault(code, [0, set(), 0, set()])
            ent[0] += TIER_PTS[tier]
            ent[1].add(rawseg)
            ent[3].add(obj_id)
            if tier == "strict":
                ent[2] += 1

    if not votes:
        return

    # Raw-only codes backed by >= 3 distinct tracked objects get a bonus
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


def _raw_skeleton_ok(raw):
    """A plausible ISO 6346 read: first 4 mostly letters, rest mostly digits.

    Rejects VL hallucinations like "22G1 26 88 95..." that the repair logic
    would otherwise manufacture into checksum-valid phantom codes.
    """
    if len(raw) not in (10, 11):
        return False
    letters = sum(1 for ch in raw[:4] if ch.isalpha())
    digits = sum(1 for ch in raw[4:] if ch.isdigit())
    return letters >= 3 and digits >= len(raw) - 4 - 1


def _to_valid_code(seg):
    """Convert a raw alnum segment into an ISO 6346 code when possible.

    Returns (code, strict): strict=True only if the code was read exactly
    as a checksum-valid string. Repairs:
      - digit/letter confusion at known positions (O<->0, I<->1, ...)
      - missing last digit (4 letters + 6 digits) -> compute check digit
      - wrong last digit -> recompute check digit (checksum fails but the
        first 10 chars are usually read correctly by the VL model)
    Repairs are gated: the raw segment must have a plausible skeleton and
    stay within edit distance 2 of the repaired code.
    """
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
        # Category letter (U/J/Z) is often misread (U<->L/I etc). Try U/J/Z,
        # checksum still gates the result (only applies to full 11-char reads).
        if len(seg) == 11:
            for cat in "UJZ":
                cand = seg[:3] + cat + seg[4:]
                if es_contenedor_valido(cand):
                    return accept(cand, False)
        return None, False

    if len(seg) == 11:
        if not normalized and es_contenedor_valido(seg):
            return seg, True
        # Trust the first 10 chars, recompute the check digit
        return accept(seg[:10] + str(_compute_check_digit(seg[:10])), False)

    # 4 letters + 6 digits: check digit missing -> compute it
    return accept(seg + str(_compute_check_digit(seg)), False)


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


# ---------------------------------------------------------------------------
# Batch event construction (sweeper): raw ingest -> offline clustering
#
# The online event lifecycle (open/close/merge/split) is gone. Detections land
# raw (event_id NULL) and this sweeper builds definitive events once the pass
# is complete, with the full time window in view. Idempotent: only rows with
# event_id NULL are considered; to reprocess an event, set its detections'
# event_id back to NULL and delete the event.
# ---------------------------------------------------------------------------

SWEEP_MATURE_SECONDS = 45   # a pass is complete after this much silence
SWEEP_PASS_GAP = 20.0       # gap without detections = pass boundary
SWEEP_REWIND_JUMP = 0.15    # normalized backward jump implying a new truck


@shared_task
def process_raw_detections():
    """Cluster mature raw detections into definitive container events."""
    from aduana.models import ContainerDetection

    mature = timezone.now() - timedelta(seconds=SWEEP_MATURE_SECONDS)
    dets = list(
        ContainerDetection.objects.filter(event__isnull=True, timestamp__lt=mature)
        .exclude(class_id=99)
        .order_by("timestamp")
        .values("id", "timestamp", "source_id", "truck_id", "class_id",
                "bbox_left", "bbox_width")
    )
    if not dets:
        return

    # Coarse temporal clustering: a gap without any detection (either camera)
    # separates two passes.
    clusters = []
    cur = [dets[0]]
    for d in dets[1:]:
        if (d["timestamp"] - cur[-1]["timestamp"]).total_seconds() > SWEEP_PASS_GAP:
            clusters.append(cur)
            cur = []
        cur.append(d)
    clusters.append(cur)

    created = 0
    for cl in clusters:
        for sub in _split_cluster_by_trajectory(cl):
            _materialize_event(sub)
            created += 1
    if created:
        logger.info("Sweeper: %d evento(s) creado(s) de %d detecciones crudas",
                    created, len(dets))


def _split_cluster_by_trajectory(dets):
    """Split a temporal cluster into individual truck passes.

    The DeepStream tracker is fallible (same truck, changing ids), so truck_id
    is never trusted alone. A physical truck moves monotonically through the
    zone per camera: a new truck is declared where the cargo position rewinds
    against the direction of travel AND the tracker id changed (corroborated).
    Input/output: detection dicts as produced by .values().
    """
    by_src = {}
    for d in dets:
        by_src.setdefault(d["source_id"], []).append(d)

    boundaries = set()
    for sid, sd in by_src.items():
        if len(sd) < 4:
            continue
        xs = [d["bbox_left"] + d["bbox_width"] / 2 for d in sd]
        # Robust direction of travel: median per-detection displacement
        # (majority moves with the truck(s); same lane = same direction).
        dxs = sorted(v for v in (xs[i] - xs[i - 1] for i in range(1, len(xs)))
                     if abs(v) > 1e-4)
        if not dxs:
            continue
        med = dxs[len(dxs) // 2]
        if abs(med) < 0.002:
            continue  # stationary / no clear motion: never split
        direction = 1 if med > 0 else -1

        prev_tid, prev_x = sd[0]["truck_id"], xs[0]
        for i in range(1, len(sd)):
            x, tid = xs[i], sd[i]["truck_id"]
            rewind = (x - prev_x) * direction < -SWEEP_REWIND_JUMP
            if tid != prev_tid and rewind:
                boundaries.add(sd[i]["timestamp"])
            prev_tid, prev_x = tid, x

    if not boundaries:
        return [dets]

    marks = sorted(boundaries)
    parts, cur, bi = [], [], 0
    for d in dets:
        while bi < len(marks) and d["timestamp"] >= marks[bi]:
            bi += 1
            if cur:
                parts.append(cur)
                cur = []
        cur.append(d)
    if cur:
        parts.append(cur)

    # Stitch back consecutive parts that share a tracker id on the same
    # source. A boundary from one camera can cut a single physical truck in
    # two on the other camera; tracker ids are unique per pass, so a shared
    # (source, truck_id) is conclusive proof of the same truck.
    stitched = []
    for part in parts:
        if stitched:
            keys_a = {(d["source_id"], d["truck_id"]) for d in stitched[-1]}
            keys_b = {(d["source_id"], d["truck_id"]) for d in part}
            if keys_a & keys_b:
                stitched[-1].extend(part)
                continue
        stitched.append(part)
    return stitched


def _materialize_event(dets):
    """Create the definitive event for one pass and run downstream analysis."""
    from aduana.models import ContainerDetection, ContainerEvent

    first_ts = dets[0]["timestamp"]
    last_ts = dets[-1]["timestamp"]

    event = ContainerEvent.objects.create(
        seal_status="processing",
        timestamp_start=first_ts,
        timestamp_end=last_ts,
    )
    det_ids = [d["id"] for d in dets]
    ContainerDetection.objects.filter(id__in=det_ids).update(event=event)

    # Full-frame snapshots (cls 99) near this pass: earliest per camera wins
    # (the first-seal trigger is the semantically right moment; the no-seal
    # fallback arrives at deactivation). Snapshot rows join the event so the
    # retention purge keeps files consistent.
    for sid in {d["source_id"] for d in dets}:
        snaps = ContainerDetection.objects.filter(
            class_id=99, source_id=sid, event__isnull=True,
            timestamp__gte=first_ts - timedelta(seconds=10),
            timestamp__lte=last_ts + timedelta(seconds=10),
        ).order_by("timestamp")
        snap = snaps.first()
        if snap:
            setattr(event, f"frame_src{sid}", snap.crop.name)
            snaps.update(event=event)
    event.save(update_fields=["frame_src0", "frame_src1"])

    _finalize_event_simple(event)


def _finalize_event_simple(event):
    """Seal status + downstream analysis for a sweeper-created event."""
    seal_detections = event.detections.filter(class_id__in=[0, 1])

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

    event.save(update_fields=["seal_status", "seal_confidence"])
    logger.info(
        "Event %s materialized: seal=%s (conf=%.2f) con=%d sin=%d dets=%d",
        event.id, event.seal_status, event.seal_confidence,
        con_sello_count, sin_sello_count, event.detections.count(),
    )

    ocr_event.delay(event.id)
    analyze_seals.delay(event.id)


# ---------------------------------------------------------------------------
# Operations: silence watchdog + data retention
# ---------------------------------------------------------------------------

WATCHDOG_SILENCE_HOURS = 2
WATCHDOG_WORK_START_HOUR = 7
WATCHDOG_WORK_END_HOUR = 20  # exclusive

RETENTION_DAYS = 14


@shared_task
def watchdog_detection_silence():
    """Raise an alarm when no detections arrive during working hours.

    The costliest failure mode of this system is silent zero-reading hours
    (dead pipeline, moved camera, dead LC/ROI geometry) with every container
    green. Runs every 15 min via Celery Beat.
    """
    from aduana.models import ContainerDetection

    now_local = timezone.localtime()
    if now_local.weekday() == 6:  # Sunday: no traffic expected
        return
    if not (WATCHDOG_WORK_START_HOUR <= now_local.hour < WATCHDOG_WORK_END_HOUR):
        return

    cutoff = timezone.now() - timedelta(hours=WATCHDOG_SILENCE_HOURS)
    last = (
        ContainerDetection.objects.order_by("-timestamp")
        .values_list("timestamp", flat=True)
        .first()
    )
    if last is not None and last >= cutoff:
        return

    fps_info = ""
    try:
        import os
        import redis as redis_lib
        r = redis_lib.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
        fps = {
            k.decode(): v.decode()
            for k, v in r.hgetall("deepstream:sources:aduana:1").items()
            if k.decode().endswith(":fps")
        }
        if fps:
            fps_info = f" FPS del pipeline (frames fluyendo): {fps}."
    except Exception:
        pass

    logger.error(
        "ALARMA: sin detecciones en las últimas %d h en horario hábil. "
        "Última detección: %s.%s Revisar pipeline/cámaras/geometría LC-ROI.",
        WATCHDOG_SILENCE_HOURS,
        last or "nunca",
        fps_info,
    )


@shared_task
def purge_old_detections():
    """Delete detections (and their crop files) older than RETENTION_DAYS.

    Events are kept (small rows: code/seal/grid history). Frame files
    referenced by old events are deleted and the fields cleared.
    """
    from django.core.files.storage import default_storage

    from aduana.models import ContainerDetection, ContainerEvent

    cutoff = timezone.now() - timedelta(days=RETENTION_DAYS)

    dets_deleted = 0
    while True:
        batch = list(
            ContainerDetection.objects.filter(created_at__lt=cutoff)
            .values_list("id", "crop")[:500]
        )
        if not batch:
            break
        for det_id, crop in batch:
            if crop:
                try:
                    default_storage.delete(crop)
                except Exception:
                    pass
        ContainerDetection.objects.filter(
            id__in=[b[0] for b in batch]).delete()
        dets_deleted += len(batch)

    frames_cleared = 0
    for ev in ContainerEvent.objects.filter(created_at__lt=cutoff):
        changed = False
        for field in ("frame_src0", "frame_src1"):
            path = getattr(ev, field)
            if path:
                try:
                    default_storage.delete(str(path))
                except Exception:
                    pass
                setattr(ev, field, "")
                changed = True
        if changed:
            ev.save(update_fields=["frame_src0", "frame_src1"])
            frames_cleared += 1

    if dets_deleted or frames_cleared:
        logger.info(
            "purge_old_detections: %d detecciones eliminadas, frames limpiados en %d eventos (> %d días)",
            dets_deleted, frames_cleared, RETENTION_DAYS,
        )


# ---------------------------------------------------------------------------
# Seal grid analysis (door positions 1-8)
# ---------------------------------------------------------------------------

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

