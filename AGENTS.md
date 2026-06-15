# AGENTS.md

## Setup & environment

- Copy `.env.example` to `.env` before building. `.env` is gitignored.
- Everything runs in Docker Compose (`docker compose up -d --build`).
- Django settings module: `config.settings`. Always set `DJANGO_SETTINGS_MODULE=config.settings` for non-service commands.

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

- **Monorepo with Docker Compose** (`docker-compose.yml`). Project name: `mediamtx-manager`.
- **Django 6.0** with Gunicorn (WSGI, port 8000) and Daphne (ASGI/WebSocket, port 8001), behind nginx on port 80.
- **Nine Django apps**: `devices` (core), `live` (WebSocket bridge), `ptz`, `detections` (face rec), `notifications` (Telegram), `incidents`, `operadores` (sites/guards), `monitoring` (system metrics).
- **Celery** with `DatabaseScheduler` — the orchestrator. Beat schedule is defined in `config/settings.py:CELERY_BEAT_SCHEDULE`.
- **PostgreSQL with pgvector** for face embeddings.
- **Redis** serves triple duty: Celery broker, Channels layer, DeepStream pub/sub cache.
- **MediaMTX** handles RTSP→WebRTC transcoding for browser viewing.
- **DeepStream** (C++, NVIDIA GPU) runs video analytics pipelines against camera streams. Multiple pipeline variants share a common Docker image but different config YAML files.

## Critical conventions

- **Device.username/password are the single source of truth** for ONVIF and RTSP auth. Never use hardcoded creds.
- **Stream URIs are used verbatim.** `Device.stream_uris[profile_token]` is the exact output of `MediaService.get_stream_uri()`. Never modify, split, strip, or reconstruct it. Only allowed transforms: percent-encoding `+` → `%2B` in MediaMTX URLs.
- **Only `Device.default_profile_token`** is used for DeepStream and MediaMTX. Other profiles are stored for reference.
- **DeepStream pipelines are static** — adding/removing cameras or editing analytics requires regenerating `config.yml` + `config_nvdsanalytics.txt` and restarting the relevant `computer-vision*` container. Use `regenerate_config_and_restart()`.
- **`orchestrate_cameras`** (Celery Beat, every 5s) is the unified orchestrator — ONVIF ping, FPS checks, auto-recovery.
- **Migrations run automatically** via `docker-entrypoint.sh` with a PostgreSQL advisory lock (`pg_advisory_lock(123456)`). Manual `python manage.py migrate` is not needed normally.
- **Startup sync**: `DevicesConfig.ready()` (in `django/devices/apps.py`) spawns a daemon thread that pings all devices via ONVIF, refreshes stream URIs, syncs MediaMTX paths, regenerates DeepStream config, and triggers a pipeline restart.
- **Generated configs are gitignored**: `computer_vision/config/config*.yml` and `computer_vision/config/config_nvdsanalytics.txt` contain credentials and must never be committed.

## Testing

- No test suite exists. Use `docker compose exec django-http python manage.py test <app>`.
- GPU-dependent features (DeepStream, face rec) cannot be tested in CI without NVIDIA hardware.

## Lint / style

- Ruff with default settings (no `ruff.toml` or `pyproject.toml` config). Run inside the container.
- Django locale: Spanish (es-cl), timezone: America/Santiago.
- Frontend: Tabler CSS framework + p5.js, served from static vendor directory.

## Service map (key containers)

| Service | Role | Port |
|---------|------|------|
| nginx | Reverse proxy | 80 |
| django-http | UI + REST API (Gunicorn) | 8000 (internal) |
| django-asgi | WebSocket (Daphne) | 8001 (internal) |
| celery-beat | Scheduler (DatabaseScheduler) | — |
| celery-worker | Async tasks | — |
| redis-event-bridge | Redis → Channels WebSocket forwarder | — |
| notification-bridge | Notification dispatch | — |
| face-receiver | TCP server for face crops + embeddings | 12348 |
| snapshot-receiver | TCP server for camera snapshots | 12349 |
| computer-vision* | DeepStream GPU pipelines | — |
| mediamtx | RTSP/WebRTC media server | 8554, 8889, 9997 |
| event-stream-service | Dahua event HTTP streaming | — |
| postgres | Database (pgvector) | 5432 |
| redis | Cache/broker/channel layer | 6379 |
