import logging

from celery import shared_task

from monitoring.collectors.mediamtx import collect_mediamtx_metrics
from monitoring.collectors.system import collect_system_metrics
from monitoring.models import MetricSnapshot

logger = logging.getLogger(__name__)

MAX_SNAPSHOTS_PER_SOURCE = 200


@shared_task
def collect_mediamtx():
    try:
        data = collect_mediamtx_metrics()
        MetricSnapshot.objects.create(source="mediamtx", data=data)
        _prune("mediamtx")
    except Exception as e:
        logger.warning("collect_mediamtx error: %s", e)


@shared_task
def collect_system():
    try:
        data = collect_system_metrics()
        MetricSnapshot.objects.create(source="system", data=data)
        _prune("system")
    except Exception as e:
        logger.warning("collect_system error: %s", e)


@shared_task
def collect_snmp():
    try:
        from monitoring.collectors.snmp import collect_snmp_metrics

        data = collect_snmp_metrics()
        if data:
            MetricSnapshot.objects.create(source="snmp", data=data)
            _prune("snmp")
    except Exception as e:
        logger.warning("collect_snmp error: %s", e)


@shared_task
def collect_deepstream():
    try:
        from monitoring.collectors.deepstream import collect_deepstream_metrics

        data = collect_deepstream_metrics()
        MetricSnapshot.objects.create(source="deepstream", data=data)
        _prune("deepstream")
    except Exception as e:
        logger.warning("collect_deepstream error: %s", e)


def _prune(source):
    cutoff = (
        MetricSnapshot.objects.filter(source=source)
        .order_by("-created_at")
        .values_list("id", flat=True)[MAX_SNAPSHOTS_PER_SOURCE:]
    )
    if cutoff:
        MetricSnapshot.objects.filter(id__in=list(cutoff)).delete()
