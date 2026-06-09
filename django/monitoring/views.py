from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render

from monitoring.models import MetricSnapshot


@login_required
def dashboard(request):
    snapshots = MetricSnapshot.objects.all().order_by("-created_at")[:200]

    latest_system = (
        MetricSnapshot.objects.filter(source="system")
        .order_by("-created_at")
        .first()
    )
    latest_mediamtx = (
        MetricSnapshot.objects.filter(source="mediamtx")
        .order_by("-created_at")
        .first()
    )

    return render(
        request,
        "monitoring/dashboard.html",
        {
            "latest_system": latest_system,
            "latest_mediamtx": latest_mediamtx,
            "snapshots": snapshots,
        },
    )


@login_required
def api_metrics(request):
    source = request.GET.get("source", "system")
    limit = int(request.GET.get("limit", 60))

    qs = (
        MetricSnapshot.objects.filter(source=source)
        .order_by("-created_at")
        .values("data", "created_at")[:limit]
    )

    data = []
    for row in reversed(list(qs)):
        data.append(
            {
                "ts": row["created_at"].isoformat(),
                "data": row["data"],
            }
        )

    return JsonResponse({"source": source, "items": data})
