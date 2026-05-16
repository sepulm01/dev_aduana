# MediaMTX Manager

Sistema web para gestionar cámaras IP ONVIF y visualizarlas en el browser vía WebRTC. Descubre dispositivos en la red, configura streams RTSP, los transcodifica a H.264, y provee control PTZ, detección de movimiento, y eventos en tiempo real vía WebSocket.

---

## Arquitectura — Contenedores

### nginx (`nginx:alpine`, puerto 80)
Reverse proxy que enruta cada tipo de tráfico al backend correcto:
- `/` → `django-http:8000` (UI + REST API)
- `/ws/` → `django-asgi:8001` (WebSockets)
- `/stream/` → `mediamtx:8889` (WebRTC player)
- `/cam_` → `mediamtx:8889` (WHEP signaling)

### django-http (`gunicorn`, puerto interno 8000)
Sirve la interfaz web (Django templates) y las APIs REST:
- CRUD de dispositivos (`/api/devices/`)
- Descubrimiento ONVIF en red local (`/api/discover/`)
- Perfiles de video y streams RTSP (`/api/devices/{id}/profiles/`)
- Control PTZ (`/api/ptz/{id}/move/`, `/preset/`, `/status/`)
- Configuración de detección de movimiento (`/api/devices/{id}/motion-config/`)
- Sincronización de hora de las cámaras (`/api/devices/{id}/sync-time/`)

### django-asgi (`daphne`, puerto interno 8001)
Maneja conexiones WebSocket persistentes (`/ws/device/{id}/`). Los clients reciben notificaciones en tiempo real de eventos de movimiento y cambios de estado del dispositivo. Usa Django Channels + channel layer (Redis).

### celery-worker
Ejecuta tareas asíncronas. La tarea principal es `poll_camera_motion`, que consulta el estado de detección de movimiento de cada cámara vía el driver correspondiente y notifica cambios a los clients WebSocket.

### celery-beat
Scheduler que dispara la tarea `poll_all_cameras` cada 5 segundos (configurable en `CELERY_BEAT_SCHEDULE` de `settings.py`).

### mediamtx (`bluenviron/mediamtx:latest-ffmpeg`)
Servidor de media streaming. Sus funciones:
- **RTSP pull**: recibe el stream RTSP de cada cámara (camino `cam_{id}_{token}`)
- **Transcodificación ffmpeg**: en cuanto el stream raw está listo, ejecuta ffmpeg para convertir a H.264 (`cam_{id}_{token}_hw`)
- **WebRTC/WHEP**: sirve el stream transcodificado al browser mediante WHEP (WebRTC HTML5 Player)
- **API REST** (puerto 9997): endpoints para crear/eliminar/listar paths de stream

### discovery-service (`./discovery`, puerto 8765)
Microservicio de descubrimiento ONVIF que corre con `network_mode: host` para acceder a la red local. Combina dos técnicas en paralelo:

- **WS-Discovery**: broadcast multicast UDP para encontrar cámaras que respondan al estándar ONVIF (rápido, ~10s)
- **Nmap**: escaneo de subred (`-T4 --open`) en puertos típicos ONVIF (80, 8080, 443, 554) para encontrar dispositivos que no responden al multicast. Cada host con puertos abiertos se verifica mediante HTTP probe al endpoint ONVIF.

Ambos resultados se fusionan y deduplican por IP. WS-Discovery tiene prioridad (aporta nombre, hardware, perfiles); Nmap complementa los que no se anuncian.

Usa Flask, `wsdiscovery` y `python-nmap`.

Endpoint `GET /discover?timeout=10` — devuelve JSON con todos los dispositivos encontrados.
Endpoint `GET /probe?host=X&port=Y` — prueba un IP específico.
Endpoint `GET /health` — health check.

### postgres (`postgres:16-alpine`)
Base de datos primaria. Almacena dispositivos, configuraciones de cámaras, perfiles, schedules de Celery Beat, etc.

### redis (`redis:7-alpine`)
Dos roles:
- **Broker Celery**: encola tareas entre beat → worker
- **Channel layer de Django Channels**: distribuye mensajes WebSocket entre instancias de django-asgi

---

## Flujo de datos

```
Cámara IP (ONVIF RTSP)
    │
    ▼  discovery + credenciales + profile token
django-http  ──  MediaMTXAPI.ensure_camera_streams()
    │
    ▼  POST /v3/config/paths/add/{cam_N_token}
mediamtx — path raw (source = RTSP de la cámara)
    │
    ▼  runOnReady → ffmpeg -i rtsp://.../cam_N_token -c:v libx264 ...
mediamtx — path _hw (source = publisher)
    │
    ▼  WHEP endpoint
Browser (vídeo iframe vía WebRTC)
```

Las flechas punteadas representan configuración inicial; una vez configurado, el flujo de video es directo cámara → mediamtx → browser.

---

## Variables de entorno

Ver `.env.example`:

| Variable | Descripción |
|---|---|
| `SECRET_KEY` | Clave secreta de Django |
| `DEBUG` | Modo debug (`True`/`False`) |
| `ALLOWED_HOSTS` | Hosts permitidos |
| `POSTGRES_DB/USER/PASSWORD/HOST` | Conexión a PostgreSQL |
| `REDIS_URL` | Conexión a Redis |
| `MEDIAMTX_API_KEY` | Clave API para autenticarse en la API REST de MediaMTX |

---

## Quick Start

```bash
cp .env.example .env
# editar .env con credenciales reales
docker-compose up -d --build
```

Abrir `http://localhost/`.
