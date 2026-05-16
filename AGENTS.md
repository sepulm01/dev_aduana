# MediaMTX Manager — Agent Guide

## Build / Lint / Test Commands

All commands run from `/var/www/dev_security/django/` with the venv activated (or use `../env/bin/python`).

```bash
cd /var/www/dev_security/django
source ../env/bin/activate
```

### Django management

| Command | Description |
|---------|-------------|
| `python manage.py runserver --noreload` | Start dev server (no auto-reload; avoids `ready()` race) |
| `python manage.py makemigrations` | Create new DB migrations |
| `python manage.py migrate` | Apply all pending migrations |
| `python manage.py createsuperuser` | Create admin user |
| `python manage.py sync_mediamtx` | Sync all camera paths to MediaMTX |
| `python manage.py sync_mediamtx --device-id N` | Sync a single device |

### Tests

Django test runner is available but no tests exist yet:

```bash
python manage.py test                    # all tests
python manage.py test devices.tests      # per-app
python manage.py test devices.tests.test_foo.TestBar.test_baz  # single test
```

### Celery

| Command | Description |
|---------|-------------|
| `celery -A config worker -l info` | Start Celery worker |
| `celery -A config beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler` | Start Celery Beat |
| `python manage.py shell` | Django shell for debugging tasks |

### Linting / Type checking

Ruff is available (`../env/bin/ruff`). No config file exists — run ad-hoc:

```bash
ruff check .                             # lint
ruff check --fix .                       # lint + auto-fix
ruff format .                            # format
```

No `mypy` or `pyright` config exists. Add `pyproject.toml` if you introduce checks.

---

## Code Style Guidelines

### Imports

Order: stdlib → blank line → Django/third-party → blank line → local. One `import` per line. Use relative imports within same app, absolute across apps.

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

- Double quotes for strings (`"text"`, not `'text'`)
- 4-space indentation
- 100-char line limit where practical
- Single blank line between functions
- No trailing semicolons
- No trailing whitespace

### Naming

- **Classes:** `PascalCase` (`OnvifClient`, `MediaMTXAPI`, `DeviceDiscovery`, `PTZService`)
- **Functions/variables:** `snake_case` (`add_device`, `stream_uri`, `profile_token`)
- **Constants:** `UPPER_SNAKE_CASE` (`MEDIAMTX_URL` in settings)
- **Private attributes:** leading underscore (`self._cam`, `self._device`)
- **Module filenames:** `snake_case.py` (`mediamtx_api.py`, `onvif_utils/`)
- **URL route names:** `snake_case` (`device_detail`, `api_discover`)

### Django Conventions

- **Use function-based views (FBVs)** exclusively — no CBVs
- Decorate API views with `@csrf_exempt` (no user auth in this project)
- Return `JsonResponse({"error": msg}, status=N)` for errors
- Use `get_object_or_404(Device, id=...)` for single-object lookup
- Use `path()` in `urls.py` (not `re_path` or `url`)
- App templates go in `app/templates/app/` (e.g. `devices/templates/devices/`)
- Templates extend `base.html` and use `{% block title %}`, `{% block content %}`, `{% block scripts %}`
- UI text is in Spanish (locale `es-cl`, timezone `America/Santiago`)
- Bootstrap 5.3.3 + Bootstrap Icons 1.11.3, dark theme (`bg-dark text-light`)
- Inline `<script>` in templates (no separate JS files)

### Error Handling

- Broad `except Exception` is accepted (ONVIF/network code is inherently fragile)
- Always `logger.warning("...", e)` before swallowing
- External HTTP calls: use `resp.raise_for_status()` + `except requests.RequestException`
- API views: return `JsonResponse({"error": str(e)}, status=500)` on unexpected errors
- Utility modules may use `print()` for errors (not ideal but established precedent)
- Validate required fields early, return `status=400` immediately
- Use `DriverError` for driver-specific errors (`from onvif_utils.drivers.base import DriverError`)

### Models & Migrations

- `default_auto_field = "django.db.models.BigAutoField"` in each `AppConfig`
- JSON fields use `models.JSONField(blank=True, default=dict)`
- CharFields use `blank=True, default=""`
- Migration names are descriptive (`0002_device_stream_uris.py`)
- `Meta.ordering` is a list, not a tuple

### MediaMTX Integration

- Stream naming: `cam_{device_id}_{profile_token}` (raw), `cam_{device_id}_{profile_token}_hw` (transcoded)
- ffmpeg: `-c:v libx264 -preset ultrafast -tune zerolatency -c:a copy`
- `runOnReady` on raw path (not `runOnInit` on HW path) to avoid race condition
- MediaMTX API at `http://127.0.0.1:9997` (`/v3/config/paths/add/{name}`, `/v3/config/paths/delete/{name}`, `/v3/config/paths/list`)
- WebRTC at `http://127.0.0.1:8889`, RTSP at `127.0.0.1:8554`
- Camera paths never hardcoded in `mediamtx.yml` — only in DB + MediaMTX REST API

### Startup

- `AppConfig.ready()` restores MediaMTX paths in a **daemon thread** (not inline — avoids `populate()` reentrancy)
- Devices without `stream_uris` are skipped during auto-restore
- Always use `--noreload` with `runserver` to prevent `ready()` double-fire

### Celery Tasks

- Tasks use `@shared_task` decorator (not `@task`)
- Use `bind=True` and `max_retries=N` for tasks that need retry logic
- Tasks communicate with WebSocket clients via `channels.layers.get_channel_layer()` + `async_to_sync`
- Celery Beat schedule defined in `config/settings.py` under `CELERY_BEAT_SCHEDULE`

### WebSocket Consumers

- Use `channels.generic.websocket.AsyncWebsocketConsumer`
- Group name format: `f"device_{device_id}"`
- Event types: `motion_event`, `device_status` (defined in `consumers.py`)
- Frontend connects to `/ws/device/{device_id}/`

### Management Commands

- Subclass `django.core.management.base.BaseCommand`
- Use `self.stdout.write()` and `self.style.SUCCESS()` / `self.style.ERROR()`
- Use `add_arguments()` to define --device-id style options

### Anti-patterns (avoid)

- Do NOT add `django.setup()` inside `AppConfig.ready()`
- Do NOT add shebang lines to Django modules
- Do NOT use `h264_nvenc` — always `libx264` for cross-compatibility
- Do NOT add inline comments (`#`) unless adding a necessary caveat