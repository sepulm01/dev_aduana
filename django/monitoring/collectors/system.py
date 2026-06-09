import logging

logger = logging.getLogger(__name__)


def collect_system_metrics():
    data = {
        "cpu": _cpu_metrics(),
        "memory": _memory_metrics(),
        "disk": _disk_metrics(),
        "gpu": _gpu_metrics(),
    }
    return data


def _cpu_metrics():
    import psutil

    percent = psutil.cpu_percent(interval=1)
    per_cpu = psutil.cpu_percent(interval=0, percpu=True)
    count = psutil.cpu_count()
    load = psutil.getloadavg()

    return {
        "percent": percent,
        "per_cpu": per_cpu,
        "count": count,
        "load_1m": round(load[0], 2),
        "load_5m": round(load[1], 2),
        "load_15m": round(load[2], 2),
    }


def _memory_metrics():
    import psutil

    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    return {
        "total_mb": round(mem.total / 1024 / 1024, 0),
        "used_mb": round(mem.used / 1024 / 1024, 0),
        "available_mb": round(mem.available / 1024 / 1024, 0),
        "percent": mem.percent,
        "swap_total_mb": round(swap.total / 1024 / 1024, 0),
        "swap_used_mb": round(swap.used / 1024 / 1024, 0),
        "swap_percent": swap.percent,
    }


def _disk_metrics():
    import psutil

    partitions = []
    for part in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(part.mountpoint)
            partitions.append(
                {
                    "mountpoint": part.mountpoint,
                    "device": part.device,
                    "total_gb": round(usage.total / 1024 / 1024 / 1024, 1),
                    "used_gb": round(usage.used / 1024 / 1024 / 1024, 1),
                    "free_gb": round(usage.free / 1024 / 1024 / 1024, 1),
                    "percent": usage.percent,
                }
            )
        except PermissionError:
            continue

    return {
        "partitions": partitions,
    }


def _gpu_metrics():
    try:
        import pynvml

        pynvml.nvmlInit()
    except Exception:
        return {"available": False}

    try:
        count = pynvml.nvmlDeviceGetCount()
        gpus = []
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)

            temp = None
            try:
                temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            except Exception:
                pass

            gpus.append(
                {
                    "index": i,
                    "name": name,
                    "memory_total_mb": round(mem.total / 1024 / 1024, 0),
                    "memory_used_mb": round(mem.used / 1024 / 1024, 0),
                    "memory_free_mb": round(mem.free / 1024 / 1024, 0),
                    "gpu_utilization_pct": util.gpu,
                    "memory_utilization_pct": util.memory,
                    "temperature_c": temp,
                }
            )

        pynvml.nvmlShutdown()
        return {"available": True, "gpus": gpus}
    except Exception as e:
        logger.warning("GPU metrics error: %s", e)
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass
        return {"available": False, "error": str(e)}
