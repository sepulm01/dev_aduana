# MediaMTX Manager — Agent Guide

## Build / Lint / Test Commands

### Docker (primary dev workflow)

```bash
docker-compose up -d --build                  # rebuild & restart all
docker-compose up -d --build django-http      # rebuild single service
docker-compose exec django-http python manage.py <cmd>
docker-compose logs -f django-http
```

### Django management (inside container or via `../env/bin/python`)

```bash
python manage.py runserver --noreload    # dev server
python manage.py makemigrations          # create DB migrations
python manage.py migrate                 # apply migrations
python manage.py sync_mediamtx           # sync camera paths to MediaMTX
python manage.py sync_mediamtx --device-id N
```

### Tests / Celery / Lint

```bash
python manage.py test                                       # all tests
python manage.py test devices.tests.test_foo.TestBar.test_baz  # single test
celery -A config worker -l info                              # worker
celery -A config beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler
ruff check . && ruff format .                                # lint + format
```

---

## Code Style Guidelines

### Imports

Order: stdlib → blank line → Django/third-party → blank line → local. One `import` per line. Use relative imports within the same app, absolute across apps.

```python
import json
import logging
import threading

from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.views.decorators.csrf import csrf_exempt
import requests
from wsdiscovery import WSDiscovery

from devices.models import Device
from onvif_utils.client import OnvifClient
from onvif_utils.mediamtx_api import MediaMTXAPI
```

### Formatting

- Double quotes for strings (`"text"`, never `'text'`)
- 4-space indentation
- 100-char line limit where practical
- Single blank line between functions, two between classes
- No trailing semicolons, no trailing whitespace

### Naming

- **Classes:** `PascalCase` (`OnvifClient`, `MediaMTXAPI`, `DeviceDiscovery`, `PTZService`, `CameraDriver`)
- **Functions/variables:** `snake_case` (`add_device`, `stream_uri`, `profile_token`)
- **Constants:** `UPPER_SNAKE_CASE` (`MEDIAMTX_URL`, `DEFAULT_CAMERA_SPECS`, `SEGMENT_RE`)
- **Private attributes:** leading underscore (`self._cam`, `self._device`, `self._auth`)
- **Module filenames:** `snake_case.py` (`mediamtx_api.py`, `onvif_utils/`)
- **URL route names:** `snake_case` (`device_detail`, `api_discover`, `ptz_move`)

### Django Conventions

- **Function-based views (FBVs)** exclusively — no CBVs anywhere
- Decorate API views with `@csrf_exempt` (no user auth in this project)
- Return `JsonResponse({"error": msg}, status=N)` for errors, `JsonResponse({"ok": True})` for success
- Use `get_object_or_404(Device, id=...)` for single-object lookup
- Use `path()` in `urls.py` (not `re_path` or `url`)
- Validate required fields early, return `status=400` immediately
- App templates go in `app/templates/app/` (e.g. `devices/templates/devices/`)
- Templates extend `base.html` and use `{% block title %}`, `{% block content %}`, `{% block scripts %}`
- UI text is in Spanish (locale `es-cl`, timezone `America/Santiago`)
- Bootstrap 5.3.3 + Bootstrap Icons 1.11.3, dark theme (`bg-dark text-light`, `bg-black border-secondary`)
- Inline `<script>` in templates (no separate JS files), with `{% load static %}` in `{% block scripts %}`
- URLs use `{% url 'name' arg %}` pattern, AJAX calls use absolute paths like `/api/devices/{{ device.id }}/profiles/`

### Error Handling

- Broad `except Exception` is accepted (ONVIF/network code is inherently fragile)
- Always `logger.warning("...", e)` before swallowing exceptions
- `except json.JSONDecodeError` for bad request bodies, return `status=400`
- External HTTP calls: use `resp.raise_for_status()` + `except requests.RequestException`
- API views: return `JsonResponse({"error": str(e)}, status=500)` on unexpected errors
- Validate required params early (e.g. `if not username: return JsonResponse(..., status=400)`)
- Use `DriverError` for driver-specific errors (`from onvif_utils.drivers.base import DriverError`)
- Utility modules may use `print()` for errors (established precedent, not ideal)

### Models & Migrations

- `default_auto_field = "django.db.models.BigAutoField"` in each `AppConfig`
- JSON fields use `models.JSONField(blank=True, default=dict)`
- CharFields use `blank=True, default=""`
- Migration names are descriptive (`0002_device_stream_uris.py`)
- `Meta.ordering` is a list, not a tuple (`["-discovered_at"]`)
- `__str__` returns a descriptive label (`f"{self.name} ({self.host})"`)

### MediaMTX Integration

- Stream naming: `cam_{device_id}_{profile_token}` (raw), `cam_{device_id}_{profile_token}_hw` (transcoded)
- ffmpeg: `-c:v libx264 -preset ultrafast -tune zerolatency -c:a copy`
- `runOnReady` on raw path triggers ffmpeg transcoding to `_hw` path
- MediaMTX API at `http://127.0.0.1:9997` (endpoints: `/v3/config/paths/add/{name}`, `/v3/config/paths/delete/{name}`, `/v3/config/paths/list`)
- WebRTC at `http://127.0.0.1:8889`, RTSP at `127.0.0.1:8554`
- Auth: Basic auth with `admin:mediamtx_admin_pass` via `MediaMTXAPI._headers()`
- RTSP URLs must be percent-encoded via `_encode_rtsp_url()` (especially `+` → `%2B`)
- Camera paths never hardcoded in `mediamtx.yml` — only in DB + MediaMTX REST API

### Startup

- `AppConfig.ready()` restores MediaMTX paths in a **daemon thread** (not inline — avoids `populate()` reentrancy)
- Devices without `stream_uris` or credentials are skipped during auto-restore
- Always use `--noreload` with `runserver` to prevent `ready()` double-fire
- `docker-entrypoint.sh` runs migrations with a pg_advisory_lock for safety

### Celery Tasks

- Tasks use `@shared_task` decorator (not `@task`)
- Use `bind=True` and `max_retries=N` for tasks that need retry logic
- Tasks communicate with WebSocket clients via `channels.layers.get_channel_layer()` + `async_to_sync`
- Celery Beat schedule defined in `config/settings.py` under `CELERY_BEAT_SCHEDULE` (currently polls every 5s)

### WebSocket Consumers

- Use `channels.generic.websocket.AsyncWebsocketConsumer`
- Group name format: `f"device_{device_id}"`
- Event types: `motion_event`, `device_status` (defined in `live/consumers.py`)
- Frontend connects to `/ws/device/{device_id}/`
- `receive()` handles `ping` → responds with `pong`

### PTZ Integration

- All PTZ endpoints under `/api/ptz/<device_id>/` (`move/`, `status/`, `preset/`)
- PTZ capability detected via `device.camera_specs.ptz_caps` (populated by `scan_device`)
- Use `PTZService` from `onvif_utils.ptz` for ONVIF PTZ operations
- Move types: `absolute`, `continuous`, `stop`
- Preset actions: `set`, `goto`, `remove`

### Driver Pattern

- Drivers live in `onvif_utils/drivers/`, extend `CameraDriver` ABC
- `get_driver(device)` factory in `drivers/__init__.py` selects by manufacturer
- Driver methods: `detect()`, `get_motion_config()`, `set_motion_config()`, `get_capabilities()`, `poll_motion()`

### nginx

- nginx config at `django/nginx.conf` — serves as reverse proxy
- Routes: `/ws/` → django-asgi:8001, `/stream/` → mediamtx:8889, `/cam_` → mediamtx:8889, `/` → django-http:8000
- Uses `resolver 127.0.0.11` for Docker DNS resolution with `set $upstream_*` variables

### Anti-patterns (avoid)

- Do NOT add `django.setup()` inside `AppConfig.ready()`
- Do NOT add shebang lines to Django modules
- Do NOT use `h264_nvenc` — always `libx264` for cross-compatibility
- Do NOT add inline comments (`#`) unless adding a necessary caveat
- Do NOT use CBVs (class-based views) — this project uses FBVs exclusively
