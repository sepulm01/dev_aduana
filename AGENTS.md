# AGENTS.md — Aduana Container Inspection

## Project overview

Sistema de inspeccion de contenedores maritimos. Usa 2 camaras RTSP simultaneas para detectar sellos (con_sello/sin_sello) y leer codigos de contenedor via YOLOv9 (4 clases) + PaddleOCR.

## Setup & environment

- Copy `.env.example` to `.env` before building. `.env` is gitignored.
- Everything runs in Docker Compose (`docker compose up -d --build`).
- Django settings module: `config.settings`. Always set `DJANGO_SETTINGS_MODULE=config.settings` for non-service commands.
- Model ONNX: `computer_vision/models/yolov9_aduana/best.onnx` (generated from `best.pt` at `/var/www/dev_piloto_aduana2/weights/`)

## Developer commands (run inside containers)

```bash
# Django manage.py
docker compose exec django-http python manage.py <cmd>

# Run all tests (none exist yet)
docker compose exec django-http python manage.py test

# Run a specific app's tests
docker compose exec django-http python manage.py test devices

# Lint & format
docker compose exec django-http ruff check .
docker compose exec django-http ruff format .

# Generate migrations (then restart to auto-migrate via entrypoint)
docker compose exec django-http python manage.py makemigrations

# Shell
docker compose exec django-http python manage.py shell

# Sync MediaMTX paths
docker compose exec django-http python manage.py sync_mediamtx

# Ensure Celery Beat heartbeat entry
docker compose exec django-http python manage.py ensure_heartbeat

# Logs per service
docker compose logs -f computer-vision
docker compose logs -f celery-worker
docker compose logs -f django-http
```

## Architecture

- **Monorepo with Docker Compose** (`docker-compose.yml`). Project name: `aduana`.
- **Django 6.0** with Gunicorn (WSGI, port 8000) and Daphne (ASGI/WebSocket, port 8001), behind nginx on port 80.
- **6 Django apps**: `devices` (core), `aduana` (container inspection), `live` (WebSocket bridge), `operadores` (sites), `monitoring` (system metrics).
- **Celery** with `DatabaseScheduler` — the orchestrator. Beat schedule is defined in `config/settings.py:CELERY_BEAT_SCHEDULE`.
- **PostgreSQL with pgvector** for embeddings storage.
- **Redis** serves triple duty: Celery broker, Channels layer, DeepStream pub/sub cache.
- **MediaMTX** handles RTSP→WebRTC transcoding for browser viewing.
- **DeepStream** (C++, NVIDIA GPU) runs YOLOv9 (4-class) container seal & code detection. Single pipeline with 2 sources (camara lateral + camara puertas).

## Critical conventions

- **Device.username/password are the single source of truth** for ONVIF and RTSP auth. Never use hardcoded creds.
- **Stream URIs are used verbatim.** `Device.stream_uris[profile_token]` is the exact output of `MediaService.get_stream_uri()`. Never modify, split, strip, or reconstruct it. Only allowed transforms: percent-encoding `+` → `%2B` in MediaMTX URLs.
- **Only `Device.default_profile_token`** is used for DeepStream and MediaMTX. Other profiles are stored for reference.
- **DeepStream pipeline is static** — changing cameras requires regenerating `config_aduana.yml` + `config_nvdsanalytics.txt` and restarting `computer-vision-aduana`. Use `regenerate_config_and_restart()`. MAX_INSTANCES=1, max 2 devices per instance.
- **`orchestrate_cameras`** (Celery Beat every 5s) is the unified orchestrator — ONVIF ping, FPS checks, auto-recovery. Lives in `django/devices/tasks.py`.
- **OCR via Celery**: `process_ocr(detection_id)` runs PaddleOCR on container_cod crops. `aggregate_ocr_results(event_id)` does majority-vote consensus.
- **Container events**: `close_stale_events` (Celery Beat every 5s) finalizes events with no recent detections.
- **Migrations run automatically** via `docker-entrypoint.sh` with a PostgreSQL advisory lock (`pg_advisory_lock(123456)`).
- **Generated configs are gitignored**: `computer_vision/config/config*.yml` and `computer_vision/config/config_nvdsanalytics.txt` contain credentials and must never be committed.

## Recent changes (Jul 2026)

- **Project renamed** from `mediamtx-manager` to `aduana`. Volume names preserved with explicit `external: true` entries.
- **ONVIF socket timeout**: `socket.setdefaulttimeout(15)` in `onvif_utils/client.py` — prevents infinite hangs.
- **add_device sync**: Now fetches stream URIs + syncs MediaMTX inline (no Celery dependency for the critical path).
- **MediaMTX persistence**: Paths now written to `mediamtx/mediamtx.yml` via YAML (not just API). Config reloaded via `docker kill -s USR1`.
- **Crop binary protocol fixed**: `object_id` changed from `f` (float/4 bytes) to `Q` (uint64_t/8 bytes) in crop-receiver header. C++ struct is 52 bytes: `IIIQ5fQI`.
- **DeepStream timestamp fix**: JSON publish now uses `time(nullptr)*1000` (epoch ms) instead of `g_get_monotonic_time()` (boot ms). Fixes 1970 dates.
- **PaddleOCR GPU**: celery-worker image based on `nvidia/cuda:12.6.0-cudnn-runtime-ubuntu24.04`. PaddlePaddle 2.6.2 + cuDNN 9.3 via symlinks. GPU inference confirmed on RTX 5060.
- **Crop images in event detail**: Added thumbnail column with click-to-expand in `event_detail.html`.

## Testing

- No test suite exists. Use `docker compose exec django-http python manage.py test <app>`.
- GPU-dependent features (DeepStream, PaddleOCR) cannot be tested in CI without NVIDIA hardware.

## Lint / style

- Ruff with default settings. Run inside the container.
- Django locale: Spanish (es-cl), timezone: America/Santiago.
- Frontend: Tabler CSS framework + p5.js, served from static vendor directory.

## Service map (key containers)

| Service | Role | Port |
|---------|------|------|
| nginx | Reverse proxy | 80 |
| django-http | UI + REST API (Gunicorn) | 8000 (internal) |
| django-asgi | WebSocket (Daphne) | 8001 (internal) |
| celery-beat | Orchestrator scheduler (DatabaseScheduler) | — |
| celery-worker | Executes orchestrator + OCR (PaddleOCR GPU) | — |
| redis-event-bridge | Redis → Channels WebSocket forwarder | — |
| crop-receiver | TCP server for container crops | 12347 |
| orchestrator | Event correlation across cameras | — |
| computer-vision-aduana | DeepStream YOLOv9 pipeline | — |
| mediamtx | RTSP/WebRTC media server | 8554, 8889, 9997 |
| postgres | Database (pgvector) | 5432 |
| redis | Cache/broker/channel layer | 6379 |
