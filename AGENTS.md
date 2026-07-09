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
- **Container events**: `close_stale_events` (Celery Beat every 5s) finalizes events with no recent detections. `_finalize_event()` runs temporal clustering to detect multi-container events (split) and checks for same-container merges across consecutive events.
- **Migrations run automatically** via `docker-entrypoint.sh` with a PostgreSQL advisory lock (`pg_advisory_lock(123456)`).
- **Generated configs are gitignored**: `computer_vision/config/config*.yml` and `computer_vision/config/config_nvdsanalytics.txt` contain credentials and must never be committed.

## Recent changes (Jul 2026)

- **Project renamed** from `mediamtx-manager` to `aduana`. Volume names preserved with explicit `external: true` entries.
- **ONVIF socket timeout**: `socket.setdefaulttimeout(15)` in `onvif_utils/client.py` — prevents infinite hangs.
- **add_device sync**: Now fetches stream URIs + syncs MediaMTX inline (no Celery dependency for the critical path).
- **MediaMTX persistence**: Paths now written to `mediamtx/mediamtx.yml` via YAML (not just API). Config reloaded via `docker kill -s USR1`.
- **Crop binary protocol fixed**: `object_id` changed from `f` (float/4 bytes) to `Q` (uint64_t/8 bytes) in crop-receiver header. C++ struct is 52 bytes: `IIIQ5fQI`.
- **DeepStream timestamp fix**: JSON publish now uses `time(nullptr)*1000` (epoch ms) instead of `g_get_monotonic_time()` (boot ms). Fixes 1970 dates.
- **PaddleOCR GPU**: celery-worker image based on `nvidia/cuda:12.6.0-cudnn-runtime-ubuntu24.04`. PaddlePaddle 2.6.2 + cuDNN 9.3 via symlinks. GPU inference confirmed on RTX 4080 and RTX 5060.
- **Crop images in event detail**: Added thumbnail column with click-to-expand in `event_detail.html`.
- **Dockerfile fixes**: WSDL symlink (`site-packages/wsdl` → `dist-packages/wsdl`) for ONVIF on Ubuntu 24.04. `libcublas.so` symlink from CUDA 12.6 targets to `/usr/local/cuda/lib64/`.
- **Deployment on remote server**: Project deployed on `172.16.150.50` (RTX 4080, 31 GB RAM). Requires `nvidia-container-toolkit` (`sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker`).
- **PaddleOCR rec model**: Must re-download if corrupted (`Cannot parse tensor desc` error). Model cache at `/root/.paddleocr/whl/rec/en/`.
- **GPU compat**: `rm -rf /usr/local/cuda-12/compat` required in Dockerfile — stale libcuda.so stub breaks GPU detection on newer drivers.
- **Orchestrator removed**: The `aduana orchestrator` service was removed (created duplicate empty events via Redis pubsub). Event correlation now fully handled by crop-receiver with a 15s window (was 5s).
- **Crop confidence filter**: C++ filter `CROP_MIN_CONFIDENCE 0.6` in pipeline_test3.cpp — crops with confidence < 0.6 discarded before TCP send.
- **OCR confidence threshold**: Raised from 0.3 → 0.6 in `process_ocr` and `aggregate_ocr_results`.
- **frame_num in detection packet**: Added `uint32_t frame_num` to CropPacket (struct now 56 bytes: `IIIQ5fIQI`). Enables grouping detections from the same frame. Python HEADER_FMT updated to `<IIIQ5fIQI`. Migration added.
- **Timestamp precision**: Changed from `time(nullptr)*1000LL` (seconds × 1000, always .000) to `std::chrono::system_clock` (real milliseconds). Detections now ordered precisely in event detail.
- **Model updated**: Replaced `best.onnx` (101 MB) with YOLOv9-E `ds_20260626` (229 MB, 68M params, 240 GFLOPS). Converted via `export_yoloV9.py` from WongKinYiu/yolov9.
- **Event detail ordering**: Changed from `source_id, class_id, timestamp` to `-timestamp` (most recent first).
- **container-code only for OCR**: Only class_id=3 (`container cod`) is sent to PaddleOCR. Seal classes are stored without OCR.
- **OCR-VL-1.6 as primary engine**: New `ocr-vl` container with PaddleOCR-VL-1.6 (0.9B VLM, BF16) on RTX 4080 GPU. Reads crops via HTTP API at `http://ocr-vl:5002/ocr` in ~400ms. 100% accuracy on crops where PaddleOCR fails. PaddleOCR kept as fallback.
- **Container code validation**: ISO 6346 checksum validation via `es_contenedor_valido()` in `aggregate_ocr_results`. Regex `[A-Z]{4}\d{7}` + weighted sum modulo 11. Filters out noise like "45G1" type codes.
- **Docling server**: `docling-server` container (ghcr.io/docling-project/docling-serve-cu130:v1.16.1) for OCR performance comparisons. RapidOCR CPU-only, ~2s/crop but reads text PaddleOCR misses.
- **Event grouping by color + gap**: Multi-signal proactive grouping in `crop_receiver.py:_find_or_create_event()`. Uses 3 signals: temporal gap (threshold 3s same-source, 5s cross-source), HSV color distance (0.25), and bbox position jump (0.3). Reactive split via temporal clustering in `_finalize_event()`, plus merge of same-container events in `_try_merge_event()`.
- **Container color extraction**: `extract_avg_hsv()` in `crop_receiver.py` computes average HSV from crop JPEG, ignoring dark (<15%V) and bright (>95%V) pixels. Stored as `dominant_color_h/s/v` FloatFields on ContainerDetection (migration 0004).
- **OCR spotting mode**: Added `/spotting` endpoint in `ocr-vl` (PaddleOCR-VL-1.6) for vertical text. Fallback chain: OCR mode → spotting mode → PaddleOCR.
- **Camera sync fix**: Streammux now configured with `live-source: 1` and `sync-inputs: 0` in generated YAML config. RTSP sources get `latency=0`, `drop-on-latency=TRUE`, `protocols=TCP` via `source-setup` signal callback in `pipeline_test3.cpp`. Eliminated 3-6s inter-camera delay caused by default rtspsrc latency=2000ms buffer and missing live-source mode. Detections now balanced 52/48% between cameras (was 57/42%).
- **Cross-source gap thresholds**: `GAP_THRESHOLD=3.0s` for same-camera gaps, `GAP_CROSS_SOURCE=5.0s` for different-camera gaps. Applied in both `crop_receiver.py` (proactive) and `tasks.py` (reactive temporal clustering).
- **Annotated video recording**: `manage.py record_annotated` captures ONVIF snapshots + overlays detection bounding boxes from recent crops. Requiere `HTTPDigestAuth` para cámaras Dahua. Output GIF se guarda en `media/recordings/`, accesible via nginx. Uso: `docker exec aduana-celery-worker-1 python3 manage.py record_annotated --duration 20 --fps 5`.
- **Native 720p video recording**: Pipeline conditional controlado por `computer_vision/config/video_output.txt` (`record=1|0`). Cuando record=1, reemplaza fakesink con `nvvideoconvert → capsfilter(NV12) → capsfilter(1280×720) → nvv4l2h264enc(2Mbps) → h264parse → filesink`. Sin tee (incompatible con NVMM). 1080p causa OOM en RTX 4080.
- **NVDS Analytics + Line Crossing**: Elemento `nvdsanalytics` insertado entre `nvtracker` y `nvosd` en el pipeline C++. Config via `config_nvdsanalytics.txt` generado por `config_generator.py:_shapes_to_nvdsanalytics()`. Frontend canvas p5.js en `/devices/<id>/analytics/` (heredado de `dev_security`) para dibujar líneas de crossing. Modelo `devices.AnalyticsPreset` almacena shapes normalizadas (0.0-1.0). Probe `analytics_lc_probe` en `pipeline_test3.cpp` lee `NvDsAnalyticsObjInfo.lcStatus` y publica JSON a Redis `aduana:lc_event`. Consumer `lc_bridge` (management command, corre en `django-http`) se suscribe al canal Redis y llama `_finalize_event()` del evento abierto más reciente. Flujo: YOLO → nvtracker → nvdsanalytics → cruce línea → Redis PUBLISH → lc_bridge → cierre evento.

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
| celery-worker | Executes orchestrator + OCR tasks | — |
| redis-event-bridge | Redis → Channels WebSocket forwarder | — |
| lc-bridge | Line crossing → event finalization (Redis pubsub) | — (runs in django-http) |
| crop-receiver | TCP server for container crops | 12347 |
| computer-vision-aduana | DeepStream YOLOv9 pipeline | — |
| mediamtx | RTSP/WebRTC media server | 8554, 8889, 9997 |
| ocr-vl | PaddleOCR-VL-1.6 (0.9B VLM, GPU) | 5002 |
| docling-server | Docling OCR (RapidOCR CPU, baseline) | 5001 |
| postgres | Database (pgvector) | 5432 |
| redis | Cache/broker/channel layer | 6379 |
