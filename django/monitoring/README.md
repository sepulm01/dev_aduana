# Monitoring App

## Overview

Sistema de telemetria para infraestructura CCTV. Collectors Celery recolectan metricas cada 30-60s y las presentan en un dashboard con KPIs, graficos Chart.js y tablas.

## Arquitectura

```
celery-beat (cada 30s/60s)
  +-- collect_system    -> psutil (CPU/RAM/disk) + pynvml (GPU)
  +-- collect_mediamtx  -> REST API /v3/paths + /v3/rtspsessions + /v3/webrtcsessions
  +-- collect_snmp      -> pysnmp v2c async a Device.snmp_enabled=True
  +-- collect_deepstream -> Redis FPS + Docker API container stats

MetricSnapshot (PostgreSQL, JSONField)
  +-- source: "system" | "mediamtx" | "snmp" | "deepstream"
  +-- data: JSON con estructura libre por collector
  +-- prune: max 200 snapshots por source

Dashboard (/monitoring/)
  +-- KPIs + Chart.js (client-side fetch cada 30s)
  +-- API: /monitoring/api/?source=system&limit=60
```

## Endpoints

| URL | Descripcion |
|-----|-------------|
| `/monitoring/` | Dashboard con KPIs, graficos y tablas |
| `/monitoring/api/?source=system&limit=60` | JSON ultimos snapshots (source = system/mediamtx/snmp) |

## SNMP Device Configuration

1. Activar SNMP en el dispositivo (camara, router, antena): v2c, community `public`, puerto 161
2. En la pagina de detalle del dispositivo en Django: toggle `SNMP Habilitado`, community, puerto
3. El collector sondea cada 60s y muestra sysDescr, sysName, sysUpTime

## Dependencies

```
psutil>=7,<8
pynvml>=12,<13
pysnmp>=7,<8
Chart.js 4.4.9 (CDN)
```

## Model

### MetricSnapshot

| Field | Type | Notes |
|-------|------|-------|
| source | CharField(50) | "system", "mediamtx", "snmp", "deepstream" |
| device_id | IntegerField (null) | FK a Device (opcional) |
| data | JSONField | Collector-specific payload |
| created_at | DateTimeField | auto_now_add, indexed |

Index: `(source, created_at)`. Prune: max 200 per source.

## Collector: System

Campos en `data`:

```json
{
  "cpu": {"percent": 25.0, "per_cpu": [10, 30, ...], "count": 48, "load_1m": 2.5, ...},
  "memory": {"total_mb": 32000, "used_mb": 13000, "available_mb": 17000, "percent": 42.0, ...},
  "disk": {"partitions": [{"mountpoint": "/", "total_gb": 440, "used_gb": 360, ...}]},
  "gpu": {"available": true, "gpus": [{"index": 0, "name": "GTX 1650", "memory_total_mb": 4096, "memory_used_mb": 1838, "gpu_utilization_pct": 99, "temperature_c": 66}]}
}
```

## Collector: MediaMTX

Campos en `data`:

```json
{
  "paths": [{"name": "cam_3_main", "ready": true, "online": true, "tracks": ["H264", "MPEG-4 Audio"], "readers_count": 1, "inbound_mb": 699.0, "outbound_mb": 827.0, "inbound_frames_in_error": 0}],
  "rtsp_sessions": [{"id": "a3cd228f...", "state": "publish", "path": "cam_3_main", "rtp_packets_sent": 3502064, "rtp_packets_lost": 180, "rtp_packets_jitter": 3298}],
  "webrtc_sessions": [{"id": "419a013a...", "state": "read", "path": "cam_3_main", "rtp_packets_sent": 1446, "rtp_packets_lost": 0}]
}
```

## Collector: SNMP

Requiere `celery-worker` con `runtime: nvidia` para acceder a la red del host.
Usa pysnmp 7.x async API (hlapi.v1arch + asyncio) desde ThreadPoolExecutor.
Consulta OIDs: sysDescr (1.1.0), sysName (1.5.0), sysUpTime (1.3.0).

Campos en `data`:

```json
{
  "devices": [{"device_id": 1, "device_name": "192.168.1.108", "host": "192.168.1.108", "sys_name": "DH-SD22204DB-GNY", "sys_descr": "DH-SD22204DB-GNY", "uptime_seconds": 931.0}]
}
```

## Collector: DeepStream

Usa el socket Unix de Docker (`/var/run/docker.sock`) + Redis para medir los pipelines.

### Fuentes de datos

| Metrica | Origen | Metodo |
|---------|--------|--------|
| FPS por pipeline | Redis `deepstream:sources:{pipeline}` -> `{source_id}:fps` | `redis.hgetall()` |
| CPU container | Docker API `GET /containers/{name}/stats?stream=false` | Unix socket HTTP |
| RAM container | Docker API stats -> `memory_stats.usage/limit` | Unix socket HTTP |
| Network RX/TX | Docker API stats -> `networks.eth0.rx_bytes/tx_bytes` | Unix socket HTTP |
| PIDs | Docker API stats -> `pids_stats.current` | Unix socket HTTP |
| Estado | Docker API `GET /containers/{name}/json` -> `State.Running/Status` | Unix socket HTTP |

### Pipelines monitoreados

```python
PIPELINES = {
    "main": "mediamtx-manager-computer-vision-1",
    "retinaface": "mediamtx-manager-computer-vision-retinaface-1",
    "yolov9": "mediamtx-manager-computer-vision-yolov9-1",
    "trafficcamnet_lpr": "mediamtx-manager-computer-vision-lpr-1",
}
```

### Campos en `data`

```json
{
  "fps": {
    "main": {"total_fps": 60, "source_count": 2, "avg_fps": 30.0, ...},
    ...
  },
  "containers": {
    "main": {"running": true, "cpu_percent": 15.8, "memory_mb": 440.3, ...},
    ...
  },
  "collected_at": 1718035200.0
}
```

### Dashboard section

- KPI row: DeepStream FPS, Containers running/total, CPU pipelines, Network in
- Chart.js: FPS por pipeline (4 lineas)
- Table: Pipeline, Estado (badge), CPU%, RAM MB, FPS, Net In MB, PIDs

## Celery Beat Schedule

```python
CELERY_BEAT_SCHEDULE = {
    "monitoring-system-every-30s": {"task": "monitoring.tasks.collect_system", "schedule": 30.0},
    "monitoring-mediamtx-every-30s": {"task": "monitoring.tasks.collect_mediamtx", "schedule": 30.0},
    "monitoring-snmp-every-60s": {"task": "monitoring.tasks.collect_snmp", "schedule": 60.0},
    "monitoring-deepstream-every-30s": {"task": "monitoring.tasks.collect_deepstream", "schedule": 30.0},
}
```

## Notes

- `pynvml` requiere `runtime: nvidia` en celery-worker (docker-compose). Sin acceso GPU, `gpu.available = false`.
- `pysnmp` 7.x rompio la API sincrona tradicional. Se usa `hlapi.v1arch` con `asyncio` via `ThreadPoolExecutor`.
- El collector DeepStream requiere montar `/var/run/docker.sock` en celery-worker (ya montado).
- Los containers detenidos (yolov9, lpr) reportan `cpu_percent=0, memory_mb=0, running=false`.
- La tabla de streams MediaMTX solo aparece si hay al menos un snapshot en la DB.
- La tabla SNMP se oculta por defecto y se muestra cuando el collector reporta dispositivos.
- `sysName` OID retorna `(none)` en muchas camaras Dahua. El collector usa `sysDescr` como fallback.
- El `CELERY_BEAT_SCHEDULE` en `settings.py` es solo referencia. Las tareas se registran en DB via `ensure_heartbeat` (usa `DatabaseScheduler`).
