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

## Recent changes (Aug 2026)

- **CropPacket v3 (64 bytes)**: agregado `uint64_t truck_id` al final del struct (`IIIQ5fIQIQ`). `crop_receiver.py` parsea y guarda en `ContainerDetection.truck_id` (migración 0006). Cada crop viaja con el ID del camión activo que lo contiene.
- **Eventos por camión**: `_find_or_create_event` agrupa por `(source_id, truck_id)` — un evento por pasada física de camión. Merge cross-cámara por ventana temporal (12s) + color HSV. Ya no se usa la heurística temporal como mecanismo principal.
- **OCR a nivel evento** (`ocr_event` task): corre al cierre del evento (`_finalize_event`), no por crop. Top-12 crops cls3 por confianza (máx 3 por object_id), segundo pase si no hay código. ~90% menos llamadas al VL que por-crop.
- **Reparación ISO 6346**: `_to_valid_code` — recomputo de dígito verificador (función determinista de los 10 primeros), normalización posicional dígito↔letra (O↔0, I↔1, S↔5, B↔8, G↔6, Z↔2), reparación de letra de categoría (pos 3 → U/J/Z si checksum valida), lecturas invertidas (`\d{6}[A-Z]{4}`). Votación: un voto por detección por código, strict=2pts/repaired=1, desempate por diversidad de lecturas crudas. Raw (checksum inválido) suma si ≥3 detecciones (etiqueta física puede tener check mal pintado — caso HLBU6192440 confirmado).
- **Grilla de sellos 2×4** (`analyze_seals` task, migración 0007 `seal_grid`): al cierre del evento asigna cada sello a su posición física en la puerta (fila superior 1-4, inferior 5-8, visto desde atrás). Algoritmo: compensación de velocidad compartida (la puerta es rígida — v = mediana de dx/dt de los tracks) que colapsa cada trayectoria a su posición canónica en t_ref, clustering 2D (dist<0.03) que fusiona fragmentos del tracker, split de filas por gap en y, columnas por x ascendente con detección de gaps. Merge de cámaras por unión (gana la de más lecturas). Sin Shapely/GIS — numpy puro. Sin cambios en C++ (usa bbox/object_id/truck_id/timestamp ya existentes). Template: grilla visual 2×4 en `event_detail.html`.
- **Frames de referencia por evento** (`capture_event_frames` task, migración 0008 `frame_src0`/`frame_src1`): al cierre extrae el mejor frame por cámara del video MP4 (el de más detecciones de sellos, desempate por cantidad total y confianza) vía `ffmpeg -ss <frame_num/20fps>`. Se guarda escalado a 1280px en `media/frames/`. Los paths de video por source se leen del `source-list` de `config_aduana_test.yml`. En producción (RTSP) no aplica — requerirá captura de frames del pipeline C++ (futuro).
- **Texto vertical**: `_run_ocr_vl` es aspect-aware — crops altos (h > 1.2×w) van primero a `/spotting` (modo para texto rotado), luego `/ocr`. Limpia tokens `<|LOC_*|>` del output de spotting.
- **ocr-vl threadpool**: `server.py` corre `model.generate` en `run_in_threadpool` — antes bloqueaba el event loop y `/health` daba timeout durante inferencia, lo que activaba falsas alarmas de OCR caído. Health check ahora tiene TTL 30s y nunca bloquea el OCR (solo alarmas).
- **Fixes críticos C++**: encoding JPEG por objeto (el batch encoder corrumpía crops 2..N — faltaban 6 bytes de header JFIF), rate-limit de crops por object_id (200ms) en vez de global (el global dejaba 1 crop por batch y mataba los sellos), expansión de bbox +30% vertical antes del encode (dígitos extremos del código ya no se cortan), timestamps con `std::chrono` (ms reales), frame-skip por fuente (balanceado), `send_all` contra writes parciales TCP, `SO_SNDTIMEO` 1s (pipeline nunca se bloquea por receiver lento), purga de `g_trucks` (60s).
- **Precisión test** (videos cam1/cam2, 15 camiones): 13/15 códigos correctos. Los 2 fallos son eventos sin detecciones cls3 del modelo. Seal classes (0/1) fluyen a Django.
- **docker-compose**: archivos fuente montados como volúmenes (`models.py`, `tasks.py`, `views.py`, `crop_receiver.py`, `urls.py`, `templates/`, `migrations/`, `ocr_vl/server.py`, `computer_vision/app/`) — los `restart` ya no pierden cambios. `computer_vision/app/` montado: el binario se compila con `make` dentro del contenedor y persiste en el host (sin rebuild de imagen). ocr-vl en puerto host 5003 (5002 lo usa yolo_server local).
- **Media serving**: `config/urls.py` sirve `/media/` en DEBUG (los crops eran 404 en :8008).
- **Deploy producción (172.16.150.50, 19 Ago)**: servidor ahora corre rama `main`. La rama `feature/ocr-aggregation-improvements` (suite de tests + `ocr_codes.py`, implementación anterior del OCR) quedó respaldada en GitHub, pendiente de merge. Migraciones: la DB remota tenía `0003_ocr_texts` sin `0002_frame_num` (recriada localmente) — se resolvió insertando el row en `django_migrations` (la columna ya existía), luego 0006–0009 aplicaron normal.
- **aduana_prod.cpp parsea `source-list` del yml** (`nvds_parse_source_list`, mismo patrón que `pipeline_test3.cpp`): antes tenía hardcodeados los MP4 de test e ignoraba `config_aduana.yml`, lo que tiraba el pipeline en producción. NUNCA hardcodear fuentes — las cámaras vienen del config generado desde la DB.
- **Engine TensorRT path quirk**: nvinfer serializa el engine a `computer_vision/config/` pero lo lee de `models/yolov9_aduana/model_b2_gpu0_fp32.engine` (path absoluto en `pgie_config.yml`). Si falta ahí, cada restart recompila (~80s). Mantener copia del engine en `models/yolov9_aduana/` en cada máquina (es GPU-específico: no copiar entre RTX 5060 y 4080).
- **django/.dockerignore**: excluye `media/` del build context (6.3 GB de crops en producción cancelaban el build). `media/` está montada como volumen en todo servicio que la usa (django-http, celery-worker, crop-receiver, nginx), así que no se pierde nada.
- **Badge de posición de sello**: `seal-pos-badge` en `event_detail.html` ya no se superpone al crop (inline-block arriba de la imagen, antes absolute top-left).
- **Deploy producción día 2 (21 Ago)**: tres bugs que dejaban producción sin lecturas: (1) probe de activación de trucks leía `NVDS_USER_FRAME_META_NVDSANALYTICS` (frame-level) en vez de `NVDS_USER_OBJ_META_NVDSANALYTICS` — los trucks nunca cruzaban/activaban; (2) `aduana_prod` enviaba CropPacket de 56B sin `truck_id` → desincronizaba el stream TCP del receiver y cada JPEG perdía 8 bytes de header JFIF (fotos corruptas + OCR vacío + truck_id basura); (3) imagen ocr-vl del servidor era pre-fix (sin gcc/python3.12-dev) → Triton no compilaba kernels → 500 en todo OCR. Además: activación ahora acepta ROI "entrada" como trigger cuando no hay líneas LC configuradas (producción no las tenía en la DB). **Regla: todo fix de `aduana_test.cpp` debe portarse a `aduana_prod.cpp`** (packet format, encode por objeto, rate limits).

## Recent changes (Jul 2026)

- **Project renamed** from `mediamtx-manager` to `aduana`. External volume names parameterized via `POSTGRES_VOLUME_NAME`/`REDIS_VOLUME_NAME` in `.env` (dev machine: `mediamtx-manager_*`; production 172.16.150.50: `aduana_*`).
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
- **ROI filtering + per-class confidence**: `pipeline_test3.cpp` filtra crops por ROI (`g_roi_configured` detecta `[roi-filtering-stream-*]` en config). Solo objetos dentro del polígono ROI se envían al crop-receiver. Sin ROI definido, se envían todos. Umbrales de confianza por clase en `computer_vision/config/confidence_thresholds.txt` (formato `class_id=threshold`). Default 0.6 para clases no definidas. Defaults actuales: cls0=0.50, cls1=0.45, cls2=0.50, cls3=0.70.
- **Crop size filter para OCR**: `CROP_MIN_OCR_BYTES=3000` en `pipeline_test3.cpp:34`. Crops de class_id=3 con JPEG <3000 bytes no se envían al crop-receiver (son ilegibles o falsos positivos). Se filtra en `send_crop()` después del encode. ~15% de crops clase 3 son descartados; todos los crops >4000 bytes producen texto legible.
- **ROI-based event lifecycle**: `crop_roi_name()` en `crop_receiver.py` usa Shapely point-in-polygon sobre shapes de `AnalyticsPreset` para asignar `detection.roi_name` ("entrada"/"salida"). `close_stale_events` cierra eventos por 3 señales: (1) último sello (class_id 0/2) no visto por 3s, (2) último crop en ROI "salida" no visto por 2s, (3) timeout 15s sin detecciones. La apertura de eventos sigue siendo cualquier crop dentro de ROI activo.

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
