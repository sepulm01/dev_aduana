import http.client
import json
import logging
import socket
import time

import redis

logger = logging.getLogger(__name__)

REDIS_HOST = "redis"
REDIS_PORT = 6379

PIPELINES = {
    "main": "mediamtx-manager-computer-vision-1",
    "retinaface": "mediamtx-manager-computer-vision-retinaface-1",
    "yolov9": "mediamtx-manager-computer-vision-yolov9-1",
    "trafficcamnet_lpr": "mediamtx-manager-computer-vision-lpr-1",
}

REDIS_SOURCES_KEYS = {
    "main": "deepstream:sources:main",
    "retinaface": "deepstream:sources:retinaface",
    "yolov9": "deepstream:sources:yolov9",
    "trafficcamnet_lpr": "deepstream:sources:trafficcamnet_lpr",
}

DOCKER_SOCK = "/var/run/docker.sock"


def collect_deepstream_metrics():
    r = _get_redis()
    fps_data = _collect_fps(r)

    container_stats = {}
    for pipeline_id, container_name in PIPELINES.items():
        container_stats[pipeline_id] = _container_stats(container_name)

    return {
        "fps": fps_data,
        "containers": container_stats,
        "collected_at": time.time(),
    }


def _collect_fps(r):
    sources = {}
    for pipeline_id, redis_key in REDIS_SOURCES_KEYS.items():
        raw = r.hgetall(redis_key)
        fps_entries = []
        for k, v in raw.items():
            if isinstance(k, bytes):
                k = k.decode()
            if isinstance(v, bytes):
                v = v.decode()
            if k.endswith(":fps"):
                source_id = k.replace(":fps", "")
                device_id = raw.get(source_id, raw.get(source_id.encode(), "?"))
                if isinstance(device_id, bytes):
                    device_id = device_id.decode()
                fps_entries.append(
                    {
                        "source_id": source_id,
                        "device_id": device_id,
                        "fps": int(v) if v.isdigit() else 0,
                    }
                )
        total = sum(e["fps"] for e in fps_entries)
        count = len(fps_entries)
        avg = round(total / count, 1) if count else 0
        sources[pipeline_id] = {
            "entries": fps_entries,
            "total_fps": total,
            "source_count": count,
            "avg_fps": avg,
        }
    return sources


def _container_stats(container_name):
    stats = _docker_get(f"/containers/{container_name}/stats?stream=false")
    info = _docker_get(f"/containers/{container_name}/json")

    result = {
        "name": container_name,
        "running": False,
        "state": "unknown",
        "cpu_percent": 0.0,
        "memory_mb": 0,
        "memory_limit_mb": 0,
        "memory_percent": 0.0,
        "network_rx_mb": 0,
        "network_tx_mb": 0,
        "pids": 0,
        "started_at": None,
    }

    if info:
        state_info = info.get("State", {})
        result["running"] = state_info.get("Running", False)
        result["state"] = state_info.get("Status", "unknown")
        result["started_at"] = state_info.get("StartedAt", "")

    if stats:
        try:
            prev_cpu = stats.get("precpu_stats", {}).get("cpu_usage", {})
            curr_cpu = stats.get("cpu_stats", {}).get("cpu_usage", {})
            prev_total = prev_cpu.get("total_usage", 0)
            curr_total = curr_cpu.get("total_usage", 0)
            system_cpu = stats.get("cpu_stats", {}).get("system_cpu_usage", 0)
            prev_system = stats.get("precpu_stats", {}).get("system_cpu_usage", 0)
            online_cpus = stats.get("cpu_stats", {}).get("online_cpus", 1)

            cpu_delta = curr_total - prev_total
            sys_delta = system_cpu - prev_system
            if sys_delta > 0 and cpu_delta > 0 and online_cpus > 0:
                result["cpu_percent"] = round(
                    (cpu_delta / sys_delta) * online_cpus * 100, 1
                )

            mem = stats.get("memory_stats", {})
            result["memory_mb"] = round(mem.get("usage", 0) / 1024 / 1024, 1)
            result["memory_limit_mb"] = round(mem.get("limit", 0) / 1024 / 1024, 1)
            if result["memory_limit_mb"] > 0:
                result["memory_percent"] = round(
                    (result["memory_mb"] / result["memory_limit_mb"]) * 100, 1
                )

            networks = stats.get("networks", {})
            if networks:
                net = list(networks.values())[0]
                result["network_rx_mb"] = round(net.get("rx_bytes", 0) / 1024 / 1024, 1)
                result["network_tx_mb"] = round(net.get("tx_bytes", 0) / 1024 / 1024, 1)

            result["pids"] = stats.get("pids_stats", {}).get("current", 0)
        except Exception as e:
            logger.warning("Container stats parse error for %s: %s", container_name, e)

    return result


def _docker_get(path):
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(DOCKER_SOCK)
        conn = http.client.HTTPConnection("localhost")
        conn.sock = sock
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        if resp.status == 200:
            return json.loads(body)
    except Exception as e:
        logger.warning("Docker GET %s: %s", path, e)
    return None


def _get_redis():
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)
