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
        {"event": event, "detections": detections, "seal_cells": _seal_cells(event)},
    )


def _seal_cells(event):
    """Build the door seal grid cells from event.seal_grid for the template.

    Physical layout (door seen from behind): top row 1-2, middle row 3-4,
    bottom row 5-8.
    """
    grid = event.seal_grid or {}
    if not grid:
        return None
    labels = {
        "con_sello": ("✔ con sello", "con_sello"),
        "sin_sello": ("✖ sin sello", "sin_sello"),
    }
    rows = []
    for row_positions in ([1, 2], [3, 4], [5, 6, 7, 8]):
        cells = []
        for pos in row_positions:
            info = grid.get(str(pos)) or {}
            status = info.get("status", "sin detección")
            label, css = labels.get(status, ("— sin detección", "sin_deteccion"))
            cells.append({
                "pos": pos,
                "label": label,
                "css": css,
                "conf": info.get("conf"),
                "n": info.get("n"),
            })
        rows.append(cells)
    return rows
