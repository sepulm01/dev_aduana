from django.core.paginator import Paginator
from django.shortcuts import render

from aduana.models import ContainerEvent


def dashboard(request):
    events = ContainerEvent.objects.select_related().order_by("-timestamp_start")
    paginator = Paginator(events, 25)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)
    return render(request, "aduana/dashboard.html", {"page_obj": page_obj})


def event_detail(request, event_id):
    event = ContainerEvent.objects.prefetch_related("detections").get(id=event_id)
    detections = event.detections.order_by("-timestamp")
    return render(
        request,
        "aduana/event_detail.html",
        {"event": event, "detections": detections},
    )
