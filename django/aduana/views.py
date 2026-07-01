from django.shortcuts import render

from aduana.models import ContainerEvent


def dashboard(request):
    events = ContainerEvent.objects.select_related().order_by("-timestamp_start")[:50]
    return render(request, "aduana/dashboard.html", {"events": events})


def event_detail(request, event_id):
    event = ContainerEvent.objects.prefetch_related("detections").get(id=event_id)
    detections = event.detections.order_by("source_id", "class_id", "timestamp")
    return render(
        request,
        "aduana/event_detail.html",
        {"event": event, "detections": detections},
    )
