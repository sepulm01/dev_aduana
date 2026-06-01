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
ruff check . && ruff format .                 # lint + format (no config file, uses defaults)
```

Available DeepStream models: `yolo-v9`, `peoplenet`, `people-facerec`, `trafficcamnet-lpd-lpr`.

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
  - Exception: `re_path()` is necessary for Channels WebSocket routing (see `live/routing.py`)
- Templates: extend `base.html`, use `{% block title/content/scripts %}`
- **UI framework:** Tabler UI (Bootstrap 5-based dark theme) — `tabler.min.css` + `ti ti-*` icons
- JS: inline `<script>` in `{% block scripts %}`, vanilla ES6+ `async/await` + `fetch()`, no jQuery
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
- pgvector: `VectorField(dimensions=512)` + `IvfflatIndex` for cosine-distance face matching

### Redis
All services connect via Docker DNS `redis://redis:6379` — never hardcode `127.0.0.1`.

## Services Architecture

| Service | Role |
|---|---|---|
| `postgres` | Database (pgvector/pgvector:pg16) |
| `redis` | Message broker + cache (redis:7-alpine) |
| `django-http` | REST API + UI (gunicorn:8000) |
| `django-asgi` | WebSockets (daphne:8001) |
| `celery-worker` | Async tasks (camera orchestrator + incident manager) |
| `celery-beat` | Scheduler (orchestrate_cameras + incident_manager every 5s) |
| `redis-event-bridge` | Redis `device:*:events` → Channels |
| `face-receiver` | TCP :12348, face crops/embeddings → `Detection` records |
| `mediamtx` | RTSP:8554, WebRTC:8889, API:9997 |
| `event-stream-service` | Dahua event streaming |
| `notification-bridge` | Redis pubsub → evalúa NotificationRules → envía Telegram/Webhook |
| `telegram-ack-poller` | Polling getUpdates → detecta inline keyboard → acknowledge/resolve |
| `computer-vision` | GPU analytics (runtime: nvidia) |
| `nginx` | Reverse proxy (:80) |

MediaMTX: stream naming `cam_{device_id}_{profile_token}`, `_hw` suffix for transcoded. ffmpeg: `-c:v libx264 -preset ultrafast -tune zerolatency -c:a copy`. API auth: `admin:mediamtx_admin_pass`. RTSP URLs must percent-encode `+` → `%2B`.

## Camera Stream Standard

### Credentials
`Device.username` / `Device.password` are the **single source of truth** for all camera access (ONVIF + RTSP). These are the ONVIF credentials discovered or entered during device setup.

ONVIF `GetStreamUri(username, password)` returns an RTSP URL with credentials embedded and `&proto=Onvif` appended — this parameter tells the camera's RTSP server to authenticate using the ONVIF user. Both DeepStream and MediaMTX must use this URL **verbatim**.

If a camera ever requires different RTSP credentials, add `rtsp_username` / `rtsp_password` fields to the `Device` model as a separate override.

### RTSP URL — verbatim only
The RTSP URL stored in `Device.stream_uris[profile_token]` is the exact output of `MediaService.get_stream_uri()`. It MUST NOT be modified, stripped, split, or reconstructed. This includes the full query string (`&unicast=true&proto=Onvif`) and embedded credentials.

**Transformations allowed (and required):**
- `onvif_utils/media.py:get_stream_uri()` — injects `username:password` into the netloc (ONVIF returns a credential-less URL)
- `onvif_utils/mediamtx_api.py:_encode_rtsp_url()` — percent-encodes special chars in credentials (`+` → `%2B`) before passing to ffmpeg

**Transformations forbidden:**
- `split()` / `replace()` / `strip()` on the URL string
- Removing or reordering query parameters
- Manually reconstructing the URL

### Profile selection
Only the `Device.default_profile_token` profile is used for DeepStream and MediaMTX. All other ONVIF profiles are stored in `stream_uris` for reference but not streamed.

### Stream flow
```
Device.username/password (ONVIF)
  └─→ MediaService.get_stream_uri(token, user, pass)
        └─→ "rtsp://user:pass@host:554/path?params&unicast=true&proto=Onvif"
              └─→ Device.stream_uris[token]  (verbatim, never modified)
                    ├─→ config.yml → DeepStream GPU (rtspsrc, nvv4l2decoder)
                    └─→ MediaMTX _hw → ffmpeg CPU → WebRTC (runOnDemand)
```

### Recovery
- **Orchestrator** (`orchestrate_cameras`, every 5s): ONVIF ping + Redis FPS check on all devices
  - FPS=0 for 12 cycles (~60s) on online device → triggers `regenerate_config_and_restart()`
  - FPS<6 for 18 cycles (~90s) on online device → triggers `regenerate_config_and_restart()`
  - Device offline > 120s → triggers `regenerate_config_and_restart()`
- **`regenerate_config_and_restart()`**: repairs MediaMTX paths via `ensure_camera_streams()`, regenerates `config.yml`, updates Redis `deepstream:sources` mapping, restarts `computer-vision` container via Docker socket
- **Startup daemon** (`apps.py`): full ONVIF refresh (profiles + URIs) for all cameras, MediaMTX path sync, config regeneration, pipeline restart

## Management Commands

| Command | App | Purpose |
|---|---|---|
| `sync_mediamtx` | devices | Recreates MediaMTX paths for cameras with credentials |
| `ensure_heartbeat` | devices | Ensures `orchestrate_cameras` periodic task in DB, cleans stale entries |
| `redis_event_bridge` | live | Daemon: subscribes Redis `device:*:events` → Channels WebSocket groups |
| `face_receiver` | live | TCP server (:12348): face `Detection` records, cosine-distance face matching, `FACE_MATCH_COOLDOWN_SECONDS` dedup |
| `notification_bridge` | notifications | Daemon: subscribes Redis `device:*:events` → evalúa NotificationRules → envía canales + crea Incidentes |
| `telegram_ack_poller` | incidents | Polling `getUpdates` cada 2s → detecta callbacks de botones inline → acknowledge/resolve incidentes |

## DeepStream

### Build & Model Selection
- NVIDIA DeepStream 8.0, CUDA 12.8, binary `/opt/computer_vision/app/pipeline-test3` (C, single file)
- Model switch: `MODEL=<profile>` env var, `entrypoint-model.sh` symlinks `/opt/models/active/` to profile
- Container: `computer-vision` (runtime: nvidia), config at `/opt/computer_vision/config/config.yml`
- `people-facerec` uses det_10g as PGIE with custom RetinaFace parser (`libnvds_retinaface_parser.so`): SGIE0=`2d106det.onnx` (106-pt landmarks, 3×192×192), SGIE1=`w600k_r50.onnx` (ArcFace 512-d, 3×112×112). Both classifier mode, operate on face bboxes (class_id=0). Redis output format: `Faces[object_id, quality_score, landmarks(212 floats), embedding(512 floats)]`. Quality gate: % of 106 landmark points within [0,1] normalized region.

### Pipeline
```
ENABLE_DISPLAY=1 → streammux → ... → nvdslogger → tiler → nvvidconv → nvosd → nveglglessink
ENABLE_DISPLAY=0 → streammux → ... → nvdslogger → fakesink
```
Analytics probe on `nvdslogger src` works in both modes. Display mode (X11 window with bounding boxes) is controlled by the `ENABLE_DISPLAY` env var (default: `1`). Set to `0` for production where no X server is available.
- `ENABLE_DISPLAY=0` uses `fakesink`, no X11 dependencies, no `nvvideo-renderer` buffer drops.
`nvdspreprocess` works in DS 8.0 — the config file must exist at the model-specific path (e.g. `models/peoplenet/config_preprocess.txt`). PGIE `process-mode: 1` means "expects preprocessed tensors from nvdspreprocess". If `nvdspreprocess` is removed, PGIE still expects tensors and will produce zero detections. `gst_pad_link()` only (not `_full`), unref pads after linking, check `!= GST_PAD_LINK_OK`. SGIE bins conditional on `secondary-gie0`/`secondary-gie1` YAML keys.

**Important**: the `else` (non-REST-server, `within_multiurisrcbin: 1`) path must include `nvtracker, queue_t` in the `gst_element_link_many` chain — same as the REST-server path. Both paths use the same pipeline elements.

### Static-source architecture
- Django generates `config.yml` from `Device.objects` via `config_generator.py`
- Pipeline starts cold with all cameras defined in `config.yml`
- On camera changes: `regenerate_config_and_restart()` → new `config.yml` → `docker restart computer-vision` (~10s downtime)
- No dynamic add/remove at runtime — cold-start static pipeline

### Stream-to-Device Mapping
- `source_id` is an auto-incremented integer matching the position in `config.yml`'s `source-list`
- Redis `deepstream:sources` hash: `{source_id} → {device_id}` mapped by Django after config gen
- Analytics probe reads `source_id` from `nvdslogger` pad, maps to `device_id` via Redis
- Each stream maps to exactly one `Device.id` — no cross-mapping

### TensorRT 10.9
- **`.tlt`/`.etlt` models INCOMPATIBLE** — UFF parser removed. Only use `_decrypted`/`_onnx` models from NGC.
- `nvinfer` auto-converts ONNX → `.engine` (`onnx-file` config key). det_10g covers face detection.
- Binary: `pipeline_test3.c`, compiled with `cc`, links `-lhiredis`. No REST server dependency.

## Celery, WebSocket, PTZ & Drivers

### Celery
- `@shared_task` (not `@task`). Beat: `orchestrate_cameras` every 5s.
- Single orchestrator (`devices/tasks.py:orchestrate_cameras`): ONVIF ping + Redis FPS check + recovery restart.
- Broadcast via `channels.layers.get_channel_layer()` + `async_to_sync` to `device_{device_id}`.
- DB schedule managed by `ensure_heartbeat` command — no duplicate entries permitted.

### WebSocket
- `AsyncWebsocketConsumer`, events: `motion_event`, `device_status`. `receive()` handles `ping`→`pong`.
- Frontend: `/ws/device/{device_id}/`, Channels routing uses `re_path()` (exception to `path()`-only rule).

### PTZ & Drivers
- PTZ: `/api/ptz/<device_id>/move|status|preset/`, detected via `device.camera_specs.ptz_caps`.
- Drivers: `onvif_utils/drivers/`, extend `CameraDriver`, `get_driver(device)` factory by manufacturer.
- Snapshot: `capture_frame_rtsp(rtsp_uri)` — RTSP TCP, 1 frame, MJPEG→base64.
- ONVIF/zeep: nested fields via `getattr()` (not dict).

### Startup
- `AppConfig.ready()` uses **daemon thread** for MediaMTX restore (avoids `populate()` reentrancy).
- Migrations: pg_advisory_lock in `docker-entrypoint.sh`. Always `--noreload` with `runserver`.

## Anti-patterns (avoid)

- Do NOT add `django.setup()` inside `AppConfig.ready()`
- Do NOT use `.tlt` or `.etlt` models — incompatible with TensorRT 10.9
- Do NOT use CBVs (class-based views) — FBVs only
- Do NOT use `h264_nvenc` — always `libx264`
- Do NOT add shebang lines to Django modules
- Do NOT add inline comments unless documenting a necessary caveat
- Do NOT hardcode Redis as `127.0.0.1` — use `redis://redis:6379`
- Do NOT use `re_path`/`url` in URLconfs — `path()` only (except Channels WebSocket routing)
- Do NOT use `gst_element_get_request_pad(tee, "sink_0")` — tee sink pad is static `"sink"`
- Do NOT modify RTSP URLs from `stream_uris` — use verbatim. Only `get_stream_uri()` (credential injection) and `_encode_rtsp_url()` (percent-encoding) are allowed transformations.

## Detections / Face Recognition

- `detections.Detection` uses pgvector `VectorField(dimensions=512)`, `IvfflatIndex` with `vector_cosine_ops`.
- Face receiver (TCP :12348): JPEG crop → `END!` marker → `FaceCropPacket` struct (40 bytes) → 512d embedding → 212d landmarks. Crops saved to `detections/crops/YYYY/MM/DD/`.
- `FaceBuffer` keeps best-scoring crop per `(device_id, object_id)` for 10s, flushed on disappearance.
- `FACE_MATCH_COOLDOWN_SECONDS = 30` — same-person detections within cooldown are skipped.
- WebSocket: broadcasts `"new_face"` (first seen), `"face_match"` with `matched_id`+`distance` (re-id).
- DeepStream C++: `redis_bridge.cpp` crop socket auto-reconnects if `face-receiver` restarts.

## Notes

- **No test suite yet** — add tests inside individual app `tests/` directories.
- **No ruff config** — uses ruff defaults (88-char line length). Add `pyproject.toml` `[tool.ruff]` to customize.
- **No typechecking** — install `django-stubs` if type checking is desired.

## Authentication

All views are protected with `@login_required`. Django auth settings:

```python
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"
```

Login page at `/accounts/login/` (Tabler UI). Logout via POST to `/accounts/logout/`.
Superuser `admin` exists. New users are created via Django admin `/admin/`.

## Apps

### `notifications`

Notification rules with multi-channel backends (Telegram, Webhook). Features:
- Filtering by device, event code, analytics trigger, minimum objects
- Preset-aware event filtering (coherent with `redis-event-bridge`)
- Cooldown between notifications + min_duration_seconds (merodeo)
- RTSP photo capture attached to Telegram messages (send_photo)
- Schedule: date range + weekly time blocks per day (vanilla JS grid)
- IncidentType linkage for escalation workflow

### `incidents`

Incident management with operator-based escalation via Sites:
- IncidentType: templates with auto_resolve_seconds, dedup_window_seconds
- Incident: status machine active → acknowledged → resolved → expired
- IncidentLog: complete audit trail
- Incident snapshot (ImageField) captured at creation time
- Dashboard with live WebRTC iframes + real-time toast alerts via WebSocket
- Detail page with full event data, objects table, analytics table, audit log

### `operadores`

Operator profiles and site-based escalation:
- OperatorProfile: OneToOne(User), escalation_level (1/2/3), cargo, personal channels
- Site: organizational grouping with shared channels + SiteEscalationLevel config
- SiteEscalationLevel: timeout_seconds per level within a site
- SiteMembership: User ↔ Site many-to-many
- Device.site FK: assign camera to a site
- Signal auto-creates OperatorProfile on User creation

### WebSocket routes

| Route | Consumer | Group | Purpose |
|---|---|---|---|
| `/ws/device/(?P<device_id>\d+)/$` | DeviceConsumer | `device_{id}` | Per-device IVS events |
| `/ws/incidents/$` | IncidentConsumer | `incidents` | Global incident alerts (toast) |

### Celery tasks

| Task | Schedule | Purpose |
|---|---|---|
| `devices.tasks.orchestrate_cameras` | 5s | ONVIF ping, FPS check, recovery |
| `incidents.tasks.incident_manager` | 5s | Process active incidents, notify, escalate, auto-resolve |
