import logging
import json

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from devices.models import Device

from .models import Detection, IdentityGroup, IdentityLog

logger = logging.getLogger(__name__)


@login_required
def detection_list(request):
    qs = Detection.objects.select_related("device").order_by("-timestamp")

    device_id = request.GET.get("device")
    if device_id:
        qs = qs.filter(device_id=device_id)

    paginator = Paginator(qs, 40)
    page = paginator.get_page(request.GET.get("page"))
    devices = Device.objects.filter(is_online=True).order_by("name")

    return render(request, "detections/detection_list.html", {
        "page_obj": page,
        "devices": devices,
        "selected_device": device_id,
    })


@login_required
def detection_detail(request, pk):
    detection = get_object_or_404(
        Detection.objects.select_related("device"), pk=pk
    )
    bbox_items = [
        ("left", detection.bbox_left),
        ("top", detection.bbox_top),
        ("width", detection.bbox_width),
        ("height", detection.bbox_height),
    ]
    return render(request, "detections/detection_detail.html", {
        "detection": detection,
        "bbox_items": bbox_items,
    })


@login_required
def identity_list(request):
    qs = IdentityGroup.objects.prefetch_related("detections__device").order_by(
        "-detection_count"
    )

    device_id = request.GET.get("device")
    if device_id:
        qs = qs.filter(detections__device_id=device_id).distinct()

    paginator = Paginator(qs, 20)
    page = paginator.get_page(request.GET.get("page"))
    devices = Device.objects.filter(is_online=True).order_by("name")

    group_data = []
    for group in page.object_list:
        first_det = group.detections.order_by("timestamp").first()
        group_data.append({
            "group": group,
            "first_det": first_det,
        })

    return render(request, "detections/identity_list.html", {
        "page_obj": page,
        "group_data": group_data,
        "devices": devices,
        "selected_device": device_id,
    })


@login_required
def identity_detail(request, pk):
    identity = get_object_or_404(
        IdentityGroup.objects.prefetch_related("detections__device"), pk=pk
    )
    detections = identity.detections.order_by("-timestamp")
    first_det = detections.order_by("timestamp").first()
    last_det = detections.first()
    devices = set(d.device.name for d in detections)

    quality_avg = 0.0
    qualities = [d.quality_score for d in detections if d.quality_score is not None]
    if qualities:
        quality_avg = sum(qualities) / len(qualities)

    return render(request, "detections/identity_detail.html", {
        "identity": identity,
        "detections": detections[:50],
        "total_count": identity.detection_count,
        "first_det": first_det,
        "last_det": last_det,
        "device_list": sorted(devices),
        "quality_avg": round(quality_avg, 3),
        "has_embedding": any(d.has_embedding for d in detections[:1]),
    })


@login_required
@csrf_exempt
def identity_reassign(request, pk):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    identity = get_object_or_404(IdentityGroup, pk=pk)
    data = json.loads(request.body) if request.body else {}
    detection_id = data.get("detection_id")
    new_group_id = data.get("new_group_id")

    if not detection_id:
        return JsonResponse({"error": "detection_id required"}, status=400)

    detection = get_object_or_404(
        Detection.objects.select_related("identity_group"), pk=detection_id
    )

    old_group = detection.identity_group
    if old_group and old_group.pk != identity.pk:
        return JsonResponse(
            {"error": "Detection belongs to a different identity"}, status=400
        )

    new_group = None
    if new_group_id:
        new_group = get_object_or_404(IdentityGroup, pk=new_group_id)

    if new_group:
        detection.identity_group = new_group
        detection.save(update_fields=["identity_group"])

        new_group.detection_count += 1
        new_group.last_seen = max(
            new_group.last_seen, detection.timestamp
        ) if new_group.last_seen else detection.timestamp
        new_group.save(update_fields=["detection_count", "last_seen"])

        IdentityLog.objects.create(
            identity_group=new_group,
            action="reassign",
            details={
                "detection_id": detection_id,
                "old_group_id": old_group.pk if old_group else None,
                "new_group_id": new_group.pk,
            },
            operator=request.user if request.user.is_authenticated else None,
        )
    else:
        detection.identity_group = None
        detection.save(update_fields=["identity_group"])

        IdentityLog.objects.create(
            identity_group=identity,
            action="reassign",
            details={
                "detection_id": detection_id,
                "old_group_id": old_group.pk if old_group else None,
                "new_group_id": None,
            },
            operator=request.user if request.user.is_authenticated else None,
        )

    if old_group and old_group.pk != (new_group.pk if new_group else None):
        old_group.detection_count -= 1
        if old_group.detection_count <= 0:
            old_group.delete()
        else:
            old_group.save(update_fields=["detection_count"])

    return JsonResponse({"ok": True})


@login_required
@csrf_exempt
def identity_merge(request, pk):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    target = get_object_or_404(IdentityGroup, pk=pk)
    data = json.loads(request.body) if request.body else {}
    source_id = data.get("source_id")

    if not source_id:
        return JsonResponse({"error": "source_id required"}, status=400)

    source = get_object_or_404(IdentityGroup, pk=source_id)

    if source.pk == target.pk:
        return JsonResponse({"error": "Cannot merge into itself"}, status=400)

    source_count = source.detection_count

    source.detections.update(identity_group=target)

    target.detection_count += source_count
    target.last_seen = max(
        target.last_seen, source.last_seen
    ) if target.last_seen else source.last_seen
    if source.first_seen and (
        target.first_seen is None or source.first_seen < target.first_seen
    ):
        target.first_seen = source.first_seen
    target.save(update_fields=["detection_count", "last_seen", "first_seen"])

    IdentityLog.objects.create(
        identity_group=target,
        action="merge",
        details={
            "source_id": source.pk,
            "target_id": target.pk,
            "transferred_count": source_count,
        },
        operator=request.user if request.user.is_authenticated else None,
    )

    source.delete()

    return JsonResponse({"ok": True, "new_count": target.detection_count})
