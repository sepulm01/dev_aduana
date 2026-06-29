# MediaMTX Manager

Sistema web para gestionar cámaras IP ONVIF con visualización WebRTC, control PTZ, y video analítica con NVIDIA DeepStream. Descubre dispositivos en la red, configura streams RTSP, los procesa con GPU y publica eventos de análisis en tiempo real vía WebSocket.

---

## Arquitectura — Contenedores

### nginx (`nginx:alpine`, puerto 80)
Reverse proxy:
- `/` → `django-http:8000` (UI + REST API)
- `/ws/` → `django-asgi:8001` (WebSockets)
- `/stream/` → `mediamtx:8889` (WebRTC player)
- `/cam_` → `mediamtx:8889` (WHEP signaling)

### django-http (`gunicorn`, puerto interno 8000)
Interfaz web (Django templates) y APIs REST:
- CRUD de dispositivos (`/api/devices/`)
- Descubrimiento ONVIF en red local (`/api/discover/`)
- Perfiles de video y streams RTSP (`/api/devices/{id}/profiles/`)
- Control PTZ (`/api/ptz/{id}/move/`, `/preset/`, `/status/`)
- Configuración de analítica: ROI, overcrowding, line-crossing, direction (`/api/devices/{id}/analytics/`)
- Sincronización de hora (`/api/devices/{id}/sync-time/`)

### django-asgi (`daphne`, puerto interno 8001)
WebSockets persistentes (`/ws/device/{id}/`). Recibe eventos de analítica desde el Redis event bridge y los forwardea al frontend en tiempo real (detecciones, eventos de ROI/LC/OC/direction, estado del dispositivo).

### celery-worker
Ejecuta `orchestrate_cameras` (disparado por celery-beat). También `refresh_device_streams` para inicializar/refrescar streams de un dispositivo.

### celery-beat
Dispara `orchestrate_cameras` cada 5 segundos (único schedule en `CELERY_BEAT_SCHEDULE`). Usa `DatabaseScheduler`.

### mediamtx (`bluenviron/mediamtx:latest-ffmpeg`)
Servidor de media streaming:
- **RTSP pull**: jala el stream RTSP de cada cámara (path `cam_{id}_{token}_hw`)
- **Transcodificación on-demand**: cuando se conecta un viewer WebRTC (WHEP), ejecuta ffmpeg con `libx264` (`preset ultrafast`, `tune zerolatency`) para transcodificar H.265 → H.264
- **WebRTC/WHEP**: sirve el stream al browser
- Solo existen paths `_hw` con `runOnDemand` — sin paths raw ni `runOnReady`

### discovery-service (`./discovery`, puerto 8765, `network_mode: host`)
Microservicio Flask para descubrimiento ONVIF:
- **WS-Discovery**: multicast UDP para cámaras ONVIF (rápido, ~10s)
- **Nmap**: escaneo de subred en puertos 80, 8080, 443, 554 con verificación HTTP del endpoint ONVIF
- Endpoints: `GET /discover`, `GET /probe`, `GET /health`

### event-stream-service
Servicio Python que mantiene conexiones HTTP streaming a eventos de cámara (Dahua) y publica eventos en Redis.

### redis-event-bridge
Daemon Django que subscribe Redis `device:*:events` y forwardea mensajes a Channels WebSocket groups.

### face-receiver (TCP :12348)
Servidor TCP que recibe crops JPEG + embeddings 512-d desde DeepStream, crea registros `Detection` con pgvector, y hace face matching con cosine-distance.

### computer-vision (`runtime: nvidia`)
Pipeline de video analítica NVIDIA DeepStream 8.0 (C++, `pipeline_test3`):
- **Modelo**: `peoplenet` (detector de personas)
- **Pipeline**: `rtspsrc → nvv4l2decoder → streammux → nvinfer(pgie) → nvtracker → nvdsanalytics → tiler → nvosd → sink`
- **Configuración**: `config.yml` + `config_nvdsanalytics.txt` generados por Django, montados en `/opt/computer_vision/config/`
- **Analítica**: ROI filtering, overcrowding, line-crossing, direction detection
- **Display**: controlado por `ENABLE_DISPLAY` (1 = X11 con bounding boxes, 0 = fakesink para producción)
- Publica FPS, detecciones, y eventos de analítica a Redis (`device:{id}:events`)
- Disponible también con modelos: `yolo-v9`, `people-facerec`, `trafficcamnet-lpd-lpr`

### postgres (`pgvector/pgvector:pg16`)
Base de datos primaria con soporte pgvector para embeddings de reconocimiento facial.

### redis (`redis:7-alpine`)
- **Broker Celery**: encola tareas beat → worker
- **Channel layer Django Channels**: distribuye mensajes WebSocket
- **Cache DeepStream**: `deepstream:sources` (source_id → device_id, fps, url), `device:*:events` (pub/sub de analítica)

---

## Flujo de datos

### Stream de video

```
Cámara IP (ONVIF RTSP)
    │
    ▼  discovery + ONVIF credentials (Device.username/password)
get_stream_uri(token, username, password)
    │
    ▼  rtsp://user:pass@host:554/path?params&unicast=true&proto=Onvif
Device.stream_uris[default_profile_token]  ← almacenado verbatim, nunca modificado
    │
    ├──→ config.yml → DeepStream GPU (rtspsrc, nvv4l2decoder)
    │      └──→ Redis: device:{id}:events (FPS, detecciones, ROI/LC/OC/direction)
    │             └──→ redis-event-bridge → Channels → Browser (WebSocket)
    │
    └──→ MediaMTX path _hw (runOnDemand)
           └──→ ffmpeg libx264 → WebRTC/WHEP → Browser (<iframe>)
```

### Analítica

```
DeepStream pipeline
    │  nvdsanalytics (config_nvdsanalytics.txt)
    ├──→ ROI: bounding box verde si objInROIcnt > 0 a nivel frame
    ├──→ Overcrowding: alerta magenta si objLCCurrCnt > object-threshold
    ├──→ Line-crossing: bounding box cyan si ocStatus != ""
    └──→ Direction: bounding box amarillo si dirStatus != ""
    │
    ▼  probe en nvdsanalytics::src
Redis device:{id}:events (JSON con objetos + analytics frame)
    ▼
redis-event-bridge → Channels WebSocket group device_{id}
    ▼
Browser: actualiza canvas en tiempo real
```

---

## Estándar de credenciales ONVIF

`Device.username` / `Device.password` son la **única fuente de verdad** para autenticación ONVIF y RTSP. Son las credenciales descubiertas o ingresadas durante el setup del dispositivo.

### RTSP URL — uso verbatim

La URL en `Device.stream_uris[profile_token]` es la salida exacta de `MediaService.get_stream_uri()` y **nunca se modifica**. Incluye credenciales embebidas y `&proto=Onvif`.

Transformaciones permitidas:
1. `onvif_utils/media.py:get_stream_uri()` — inyecta `username:password` en el netloc
2. `onvif_utils/mediamtx_api.py:_encode_rtsp_url()` — percent-encode de caracteres especiales (`+` → `%2B`) antes de pasarlo a ffmpeg

Transformaciones prohibidas: `split()`, `replace()`, `strip()`, reordenar parámetros, reconstruir manualmente la URL.

### Perfil usado

Solo `Device.default_profile_token` se usa para DeepStream y MediaMTX. Los demás perfiles se almacenan en `stream_uris` como referencia.

---

## Orquestador unificado

`orchestrate_cameras` (celery-beat, cada 5s) es el único orquestador del sistema.
Vive en dos contenedores: `celery-beat` agenda la tarea y `celery-worker` la ejecuta.

1. **ONVIF ping**: `driver.ping()` (GetDeviceInformation) a cada cámara con credenciales
   - `online=True` → `failure_count=0`, `is_online=True`, `last_seen=now`, broadcast WebSocket si cambió estado
   - `online=False` → `failure_count++`, si ≥3 → `is_online=False`, broadcast WebSocket
2. **FPS check** (Redis): para dispositivos online con source_id en DeepStream
   - `FPS=0` × 12 ciclos (~60s) → restart
   - `FPS<6` × 18 ciclos (~90s) → restart
   - `offline > 120s` → restart
3. **Recuperación**: `regenerate_config_and_restart()` repara paths MediaMTX, regenera todos los `config*.yml` y `config_nvdsanalytics.txt` para los 4 pipelines × 4 instancias, actualiza Redis `deepstream:sources:{pipeline}:{instancia}`, y reinicia o para contenedores `computer-vision*` vía Docker socket según necesiten cámaras (~10s downtime)

---

## Pipeline estático (cold-start)

DeepStream no soporta add/remove dinámico de cámaras. Para minimizar el downtime al agregar/quitar cámaras, el sistema usa **múltiples instancias por pipeline**: las cámaras se distribuyen round-robin entre hasta 4 instancias, cada una en su propio contenedor con su propio archivo de configuración.

### Pipelines e instancias

| Pipeline | Contenedores | Configs | Máx cámaras/instancia | Máx batch |
|---|---|---|---|---|
| `main` (PeopleNet) | `computer-vision`, `-2`, `-3`, `-4` | `config.yml`, `config_2..4.yml` | 3 | 3 |
| `retinaface` | `computer-vision-retinaface`, `-2..4` | `config_retinaface.yml`, `_2..4.yml` | 1 | 1 |
| `yolov9` | `computer-vision-yolov9`, `-2..4` | `config_yolov9.yml`, `_2..4.yml` | 3 | 3 |
| `trafficcamnet_lpr` | `computer-vision-lpr`, `-2..4` | `config_trafficcamnet_lpr.yml`, `_2..4.yml` | 1 | 1 |

Total: 4 pipelines × 4 instancias = 16 contenedores definidos en `docker-compose.yml`.
El orquestador **para automáticamente las instancias sin cámaras** (`docker stop` vía socket).
En producción con 2 cámaras, solo 2 de los 16 contenedores están corriendo; los otros 14 están detenidos.

### Archivos de configuración

| Archivo | Generado por | Descripción |
|---|---|---|
| `config*.yml` (por pipeline × instancia) | `config_generator.generate_all_configs()` | Source list, streammux, pgie, analytics, osd, tiler, sink |
| `config_nvdsanalytics.txt` | `config_generator.generate_nvdsanalytics_config()` | ROI-filtering, overcrowding, line-crossing, direction-detection por stream |
| `config_tracker_IOU.yml` | Estático en `computer_vision/config/` | Configuración del tracker IOU |

### Mapping source_id → device_id

Redis hash `deepstream:sources:{pipeline}:{instancia}`: `{source_id} → device_id`, más sub-keys `{source_id}:camera_id`, `{source_id}:fps`, `{source_id}:url`. El source_id es el índice en el source-list de cada `config*.yml`.

---

## Analítica (nvdsanalytics)

La configuración de analítica se deriva de `AnalyticsPreset.shapes` y se convierte a formato nvdsanalytics:

| Forma | Tipo | Resultado |
|---|---|---|
| Polygon | RF (ROI Filtering) | Bounding box verde en objetos dentro del ROI |
| Polygon | OC (Overcrowding) | Alerta cuando objLCCurrCnt > `object-threshold` (default: 3) |
| Line | cross (Line-crossing) | Bounding box cyan cuando ocStatus != "" |
| Line | direction (Direction) | Bounding box amarillo cuando dirStatus != "" |

El preset de analítica se identifica con `preset_token="__fixed__"`. La interfaz permite dibujar shapes sobre un snapshot de la cámara y aplicarlos con el botón "Aplicar a IA".

---

## Reconocimiento facial

Disponible con el modelo `people-facerec`:
- PGIE: `det_10g` (detección de rostros) con parser `libnvds_retinaface_parser.so`
- SGIE0: `2d106det.onnx` (106 landmarks, 3×192×192)
- SGIE1: `w600k_r50.onnx` (ArcFace embedding 512-d, 3×112×112)
- Face receiver (TCP :12348): recibe crops + embeddings, guarda en DB con pgvector `VectorField(dimensions=512)`, matching por cosine-distance
- Face buffer: mejor crop por `(device_id, object_id)` durante 10s
- Cooldown: 30s entre matches de la misma persona

---

## Recuperación y alta disponibilidad

| Mecanismo | Trigger | Acción |
|---|---|---|
| Startup daemon | `apps.py` al arrancar Django | ONVIF refresh de todos los perfiles + URIs, sync MediaMTX, regeneración de configs, restart de pipelines |
| Orchestrator | FPS=0 × 60s / FPS<6 × 90s / offline > 120s | `regenerate_config_and_restart()` — regenera configs para todas las instancias, reinicia las que tienen cámaras, para las vacías |
| Aplicar IA | Usuario guarda shapes de analítica | `regenerate_config_and_restart()` |
| refresh_device_streams | Llamado en startup y manualmente | ONVIF refresh + snapshot + `regenerate_config_and_restart()` |

---

## Modelos disponibles

Controlados por variable de entorno `MODEL` en docker-compose:

| Modelo | Propósito |
|---|---|
| `peoplenet` | Detección de personas + nvdsanalytics (ROI/LC/OC/direction) |
| `yolo-v9` | Detección general YOLO v9 |
| `people-facerec` | Detección de personas + reconocimiento facial |
| `trafficcamnet-lpd-lpr` | Detección vehicular + patentes |

---

## Variables de entorno

Ver `.env.example`:

| Variable | Descripción |
|---|---|
| `SECRET_KEY` | Clave secreta de Django |
| `DEBUG` | Modo debug (`True`/`False`) |
| `ALLOWED_HOSTS` | Hosts permitidos |
| `ENABLE_DISPLAY` | Controla renderizado X11 en DeepStream (1=dev con bounding boxes, 0=prod fakesink) |
| `POSTGRES_DB/USER/PASSWORD/HOST` | Conexión a PostgreSQL |
| `REDIS_URL` | Conexión a Redis (`redis://redis:6379/0`) |
| `MEDIAMTX_API_KEY` | Autenticación API REST de MediaMTX |

---

## Quick Start

```bash
cp .env.example .env
# editar .env con credenciales reales
docker-compose up -d --build
```

Abrir `http://localhost/`.

---

## Comandos de gestión

```bash
# Shell Django
docker-compose exec django-http python manage.py <cmd>

# Sincronizar paths MediaMTX para cámaras con credenciales
docker-compose exec django-http python manage.py sync_mediamtx

# Asegurar entrada única de Celery Beat
docker-compose exec django-http python manage.py ensure_heartbeat

# Tests
docker-compose exec django-http python manage.py test

# Lint
ruff check . && ruff format .

# Logs
docker-compose logs -f computer-vision    # DeepStream
docker-compose logs -f celery-beat        # Scheduler
docker-compose logs -f celery-worker      # Tareas asíncronas
docker-compose logs -f django-http        # API/UI
```
