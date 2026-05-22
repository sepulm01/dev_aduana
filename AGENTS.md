# MediaMTX Manager — Agent Guide

## Build / Lint / Test Commands

```bash
# Docker (primary dev workflow)
docker-compose up -d --build                  # rebuild & restart all
docker-compose up -d --build django-http      # rebuild single service
docker-compose exec django-http python manage.py <cmd>
docker-compose logs -f django-http
docker-compose down --remove-orphans          # cleanup

# DeepStream C++
docker-compose build deepstream               # rebuild C++ app
MODEL=yolo-v9 docker-compose up -d deepstream  # start with specific model
docker-compose logs -f deepstream              # tail logs

# Django management (inside container)
python manage.py runserver --noreload         # dev server
python manage.py makemigrations && python manage.py migrate
python manage.py sync_mediamtx                # sync camera paths to MediaMTX

# Tests / Celery / Lint
python manage.py test                         # all tests
python manage.py test app.tests.test_file.TestClass.test_name  # single test
celery -A config worker -l info
ruff check . && ruff format .                 # lint + format
```

Available DeepStream models: `yolo-v9`, `peoplenet`, `people-facerec`, `trafficcamnet-lpd-lpr`.

`people-facerec` extends peoplenet with:
- SGIE0: `2d106det.onnx` (106-point facial landmarks, 3×192×192, classifier mode)
- SGIE1: `w600k_r50.onnx` (ArcFace 512-d embeddings, 3×112×112, classifier mode)
- Both SGIE operate on face bboxes (peoplenet class_id=2) in `network-type: 1` (classifier)
- Shares peoplenet engine/ONNX via symlinks, no duplication
- Redis output includes `"Faces"` array with `object_id`, `quality_score`, `landmarks` (212 floats), `embedding` (512 floats)
- Quality gate: percentage of 106 landmark points within [0,1] normalized crop region
- Both SGIE engines auto-compile on first run (2d106det: ~25s, w600k_r50: ~36s), cached to .engine files

---

## Code Style Guidelines

### Imports
Order: stdlib → blank line → Django/third-party → blank line → local. One `import` per line. Relative imports within the same app, absolute across apps.

### Formatting
- Double quotes (`"text"`, never `'text'`), 4-space indent, 100-char line limit where practical
- Single blank line between functions, two between classes
- No trailing semicolons, no trailing whitespace
- **No inline comments** — only add `#` when documenting a necessary caveat

### Naming
- **Classes:** `PascalCase` (`OnvifClient`, `PTZService`, `CameraDriver`)
- **Functions/variables:** `snake_case` (`add_device`, `stream_uri`)
- **Constants:** `UPPER_SNAKE_CASE` (`MEDIAMTX_URL`, `DEFAULT_CAMERA_SPECS`)
- **Private attributes:** leading underscore (`self._cam`, `self._device`)
- **Module files/URL routes:** `snake_case` throughout

### Django Conventions
- **FBVs exclusively** — no class-based views
- API views: decorate with `@csrf_exempt` (no user auth), return `JsonResponse`
- Errors: `JsonResponse({"error": msg}, status=N)`, success: `JsonResponse({"ok": True})`
- Lookups: `get_object_or_404(Model, id=...)`, validate required params early → `status=400`
- URLs: use `path()` (not `re_path`/`url`), `{% url 'name' arg %}`, AJAX with absolute paths
- Templates: extend `base.html`, use `{% block title/content/scripts %}`
- **UI framework:** Tabler UI (Bootstrap 5-based dark theme) — `tabler.min.css` + `ti ti-*` icons
- Theme: `data-bs-theme="dark"` on `<html>`, font: Inter via Google Fonts
- JS: inline `<script>` in `{% block scripts %}`, vanilla ES6+ `async/await` + `fetch()`, no jQuery/frameworks
- Locale: `es-cl`, timezone: `America/Santiago`, UI text in Spanish

### Error Handling
- Broad `except Exception` accepted (ONVIF/network code is fragile)
- Always `logger.warning("...", e)` before swallowing
- `except json.JSONDecodeError` → `status=400`; `except requests.RequestException` for HTTP calls
- `DriverError` from `onvif_utils.drivers.base` for driver-specific errors

### Models
- `default_auto_field = "django.db.models.BigAutoField"` in each `AppConfig`
- `JSONField(blank=True, default=dict)`, `CharField(blank=True, default="")`
- Descriptive migration names, `Meta.ordering` as list, `__str__` as `f"{self.name} ({self.host})"`

### Redis
All services connect via Docker DNS `redis://redis:6379` — never hardcode `127.0.0.1`.

---

## Services Architecture

| Service | Role |
|---|---|
| `django-http` | REST API + UI (gunicorn:8000) |
| `django-asgi` | WebSockets (daphne:8001) |
| `celery-worker` | Async tasks (poll motion) |
| `celery-beat` | Scheduler (every 5s) |
| `redis-event-bridge` | Redis `device:*:events` → Channels |
| `mediamtx` | RTSP:8554, WebRTC:8889, API:9997 |
| `discovery-service` | WS-Discovery + nmap ONVIF (Flask:8765) |
| `event-stream-service` | Dahua event streaming |
| `deepstream` | GPU analytics (runtime: nvidia) |
| `nginx` | Reverse proxy (:80) |

MediaMTX: stream naming `cam_{device_id}_{profile_token}`, `_hw` suffix for transcoded. ffmpeg: `-c:v libx264 -preset ultrafast -tune zerolatency -c:a copy`. API auth: `admin:mediamtx_admin_pass`. RTSP URLs must percent-encode `+` → `%2B`.

---

## DeepStream

### Build & Model Selection
- Compiled inside `nvcr.io/nvidia/deepstream:8.0-gc-triton-devel`, CUDA 12.8
- Binary: `/opt/deepstream-app/bridge/deepstream-server-app`
- Model switch: `MODEL=<profile> docker-compose up -d deepstream` → `entrypoint-model.sh` symlinks `/opt/models/active/` to profile
- **Video test path:** `/opt/videos/` (host `/var/www/dev_security/videos/` mounted ro)
- **PERF_MODE=1** for headless benchmarking: `nvmultiurisrcbin → nvdslogger → fakesink` (no inference)

### Pipeline
```
PGIE only:       nvmultiurisrcbin → streammux → identity → nvinfer → tiler → nvosd → sink
PGIE+SGIE0+SGIE1: ... → identity → nvinfer(pgie) → nvinfer(sgie0) → nvinfer(sgie1) → tiler → nvosd → sink
```
`nvdspreprocess` broken in DS 8.0 → use `identity` as passthrough. SGIE bins added conditionally in C++ when `secondary-gie0`/`secondary-gie1` keys exist in YAML config. `gst_pad_link()` only (not `_full`), unref pads after linking, check `!= GST_PAD_LINK_OK`.

### TensorRT 10.9 Compatibility
- **`.tlt` / `.etlt` models are INCOMPATIBLE** — UFF parser removed in TRT 10.9
- **Only use `_decrypted` or `_onnx` suffix models from NGC**
- `nvinfer` auto-converts ONNX → `.engine` at runtime (`onnx-file` config key)
- Working models: `peoplenet` (794 MB ONNX), `trafficcamnet-lpd-lpr` (3x ONNX), `yolo-v9` (pre-compiled engine)
- peoplenet already covers face detection — no separate FaceDetect model needed

### REST API
21 endpoints on port 8080 (when `within_multiurisrcbin: 0`). When `within_multiurisrcbin: 1`, REST runs inside nvmultiurisrcbin element. Key endpoints: `GET /health/get-dsready-state`, `POST /stream/add`, `POST /stream/remove`, `POST /app/quit`. Full list in `deepstream-service/bridge/README`.

---

## Celery & WebSocket

- Tasks: `@shared_task` (not `@task`), `bind=True` + `max_retries=N` for retry
- Broadcast: `channels.layers.get_channel_layer()` + `async_to_sync` to group `device_{device_id}`
- Beat schedule: `poll_all_cameras` every 5s in `CELERY_BEAT_SCHEDULE`
- Consumers: `AsyncWebsocketConsumer`, events: `motion_event`, `device_status`
- Frontend connects `/ws/device/{device_id}/`, `receive()` handles `ping` → `pong`

## PTZ, Drivers, Startup

- PTZ: `/api/ptz/<device_id>/move|status|preset/`, detected via `device.camera_specs.ptz_caps`
- Drivers: `onvif_utils/drivers/`, extend `CameraDriver`, `get_driver(device)` factory by manufacturer
- Snapshot: `onvif_utils/snapshot.py::capture_frame_rtsp(rtsp_uri)` — RTSP TCP, 1 frame, MJPEG→base64
- ONVIF/zeep: nested fields accessed via `getattr()` (not dict), e.g. `getattr(getattr(status, 'Position', None), 'PanTilt', None)`
- Startup: `AppConfig.ready()` uses **daemon thread** for MediaMTX restore (avoids `populate()` reentrancy)
- Migrations: run with pg_advisory_lock in `docker-entrypoint.sh`
- Always `--noreload` with `runserver` to prevent `ready()` double-fire

---

## Anti-patterns (avoid)

- Do NOT add `django.setup()` inside `AppConfig.ready()`
- Do NOT use `.tlt` or `.etlt` models — incompatible with TensorRT 10.9
- Do NOT use CBVs (class-based views) — FBVs only
- Do NOT use `h264_nvenc` — always `libx264`
- Do NOT add shebang lines to Django modules
- Do NOT add inline comments unless documenting a necessary caveat
- Do NOT hardcode Redis as `127.0.0.1` — use `redis://redis:6379`
- Do NOT use `re_path` or `url` in URLconfs — `path()` only
- Do NOT use `gst_element_get_request_pad(tee, "sink_0")` — tee sink pad is static `"sink"`
