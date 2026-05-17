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


## BORRADOR


### Cámaras on line

El sistema tiene 3 mecanismos principales para determinar si una cámara está activa:
1. is_online — ONVIF en startup (devices/apps.py:11-52)
- Cuando Django arranca, un daemon thread en AppConfig.ready() itera todas las cámaras con credenciales y hace client.get_device_info() (ONVIF SOAP)
- Si responde → is_online=True, si falla → is_online=False
- Solo se actualiza en startup, no durante la vida de la app
2. motion_active — Celery Beat cada 5s (devices/tasks.py:15-57)
- poll_all_cameras dispatchea poll_camera_motion para cada dispositivo
- El task llama driver.poll_motion() — en Dahua es un CGI call a eventManager.cgi?action=getCurrentEvents
- Si la respuesta contiene "VideoMotion" → motion_active=True
- Esto no actualiza is_online — solo el campo motion_active
3. event_listener_enabled flag (devices/views.py:402-417)
- Es un switch de control (no de estado real) — indica si el event-stream-service externo debería conectarse a la cámara
- El servicio externo mantiene una conexión HTTP streaming persistente (eventManager.cgi?action=attach&codes=[All]&heartbeat=30) con backoff de reconnect
- No actualiza is_online — solo publica eventos a Redis y los guarda en DeviceEvent
Observación notable
El campo last_seen en el modelo nunca se actualiza en ningún lugar del código — existe pero está inactivo.
Lo que NO hay
- No hay un mecanismo de heartbeat periódico que actualice is_online durante runtime
- device_status en el consumer de Channels está definido pero nunca se dispara activamente
- Si una cámara cae después del startup, is_online seguiría True hasta el próximo reinicio de Django

Cambios realizados
1. CameraDriver.ping() en base.py
def ping(self):
    return {"online": False, "last_seen": None}
Método genérico en el ABC — cualquier driver lo implementa.
2. DahuaDriver.ping() en dahua.py
def ping(self):
    client = OnvifClient(host, port, username, password)
    client.get_device_info()
    return {"online": True, "last_seen": datetime.now(timezone.utc)}
Usa ONVIF GetDeviceInformation (estándar, funciona con cualquier marca).
3. Nuevo campo failure_count en Device (models.py:20)
failure_count = models.IntegerField(default=0)
+ migración 0007_device_failure_count.py aplicada.
4. poll_camera_motion modificado (tasks.py)
- Éxito del ping: failure_count=0, is_online=True, last_seen=ahora
- Fallo del ping: failure_count++, si >= 3 → is_online=False
- Broadcast: cuando is_online cambia, envía device_status al canal Channels (device_{id})
Flujo completo
poll_all_cameras (cada 5s)
  └── poll_camera_motion(device_id)
        ├── driver.ping()  → GetDeviceInformation ONVIF
        │     online=True  → last_seen=now, failure_count=0, is_online=True (si cambió)
        │     online=False → failure_count+=1, si >=3 → is_online=False, broadcast
        └── driver.poll_motion()  → motion_active (sin cambio de is_online)


## Flujos de video 

El recorrido del video
Cámara (RTSP H.265/H.264)
  → ONVIF get_stream_uri() obtiene la URL RTSP
  → MediaMTX crea path: cam_{id}_{profile}
  → runOnReady ejecuta FFmpeg que suscribe internamente al raw stream
  → FFmpeg transcodifica H.265→H.264 en CPU (libx264, preset ultrafast)
  → Output a path: cam_{id}_{profile}_hw
  → nginx Proxy → WebRTC → Browser
Perfiles: ¿1 stream o varios?
Cada perfil activo = 1 conexión RTSP separada desde MediaMTX hacia la cámara.
Si tenés 3 perfiles (profile0, profile1, profile2):
- MediaMTX abre 3 conexiones RTSP simultáneas hacia la cámara
- Cada una con su propio stream (diferente resolución/bitrate típicamente)
- FFmpeg corre 3 procesos separados (uno por perfil)
- En el browser solo se ve 1 a la vez (la que seleccionás en el dropdown)

Recursos
Recurso	Uso
CPU transcoding	FFmpeg libx264 ultrafast — bajo consumo pero multiplicado por perfiles
RTSP conexiones	Una por perfil activo — si hay 10 cámaras con 3 perfiles = 30 conexiones RTSP
Ancho de banda	Cada stream RTSP consume bandwidth de la cámara al servidor
Memoria	Cada proceso FFmpeg (~50-100MB估算)
Lo que NO existe
- Sin monitoreo de recursos — no hay tracking de CPU/bandwidth/capacidad
- Sin auto-scaling — si la carga excede, no hay fallback
- Sin métricas — MediaMTX tiene endpoint /metrics pero no se usa


# Video Analítica

Plan: Integración Django ↔ DeepStream
Arquitectura
Cámaras (3) ──RTSP──→ DeepStream (Docker GPU)
                    │
                    ├───→ Redis: device:{id}:events ──→ redis_event_bridge ──→ Channels ──→ WebSocket
                    │
                    └─── subscribe: deepstream:commands
Django ──→ Redis: deepstream:commands (add/remove camera)
Componentes a crear
1. deepstream-service/ — Aplicación DeepStream
deepstream-service/
├── Dockerfile
├── requirements.txt
├── config/
│   ├── deepstream.yml          # Cámaras, modelos, thresholds
│   └── models/                 # Tus modelos entrenados
├── app/
│   ├── __init__.py
│   ├── pipeline_manager.py     # GStreamer pipeline por cámara
│   ├── redis_bridge.py         # Publica a Redis / subscribe commands
│   ├── analytics.py            # Post-procesamiento detecciones
│   └── main.py                 # Entry point
└── docker-compose.ds.yml       # Compose para el servicio GPU

2. devices/management/commands/deepstream_control.py — Comando Django

```
# Usage:
# python manage.py deepstream_control add 1 rtsp://192.168.1.108:554/stream1
# python manage.py deepstream_control remove 1
# python manage.py deepstream_control status
```

Publica mensajes a Redis deepstream:commands.


3. Extender Device model (opcional)
Agregar campo deepstream_enabled = BooleanField(default=False) para indicar qué cámaras procesa DeepStream.

4. Frontend — mostrar eventos analytics
El consumer ivs_event ya existe. Solo necesitás que el frontend muestre los eventos de detección (ya que llegan por el mismo WebSocket).
Canales Redis
Channel	Direction	Formato
deepstream:commands	Django → DeepStream	{"action": "add", "camera_id": 1, "rtsp_uri": "...", "models": ["yolo_person", "yolo_car"]}
device:{id}:events	DeepStream → Django	{"code": "SmartMotionHuman", "action": "Start", "data": {"objects": [{"class": "person", "confidence": 0.95}]}, "timestamp": "..."}
deepstream:status	DeepStream → Django	{"cameras": [1,2,3], "gpu_usage": 0.65, "fps_avg": 28} (opcional)
Manejo de caídas y recuperaciones
DeepStream side:
- GStreamer rtspsrc tiene latency=200 y reintento automático
- Sink de disconnected reconnect después de timeout=30
- Al reconectar, limpia buffer y reanuda inferencia
Django side:
- Tu sistema existente de is_online + failure_count + Celery polling sigue funcionando
- Si cámara cae, Celery marca is_online=False después de 3 polls fallidos
- DeepStream también detecta caída → publica code: "camera_offline" a Redis
- Ambos mecanismos son independientes y complementarios

Para implementar
1. Prime: Dockerfile de DeepStream + pipeline_manager.py básico que conecte 3 cámaras
2. Segundo: redis_bridge.py en DeepStream para publicar eventos
3. Tercero: Comando Django deepstream_control
4. Cuarto: Extender frontend para mostrar detecciones (badge, log)


Implementación completa. Resumen de lo creado:
Estructura
deepstream-service/
├── Dockerfile
├── requirements.txt
├── config/
│   └── deepstream.yml              # Cámaras, modelos, redis config
└── app/
    ├── __init__.py
    ├── pipeline_manager.py         # GStreamer pipelines por cámara
    ├── redis_bridge.py             # Subscribe comandos, publish eventos
    ├── analytics.py                # Procesamiento de detecciones
    └── main.py                     # Entry point
django/devices/management/commands/
└── deepstream_control.py           # Comando para enviar comandos a DeepStream
Uso
Enviar comando add (agregar cámara en caliente):
docker-compose exec django-http python manage.py deepstream_control add 1 \
    --rtsp-uri "rtsp://admin:admin@192.168.1.108:554/stream1" \
    --models "person_detection,vehicle_detection"
Quitar cámara:
docker-compose exec django-http python manage.py deepstream_control remove 1
Estado:
docker-compose exec django-http python manage.py deepstream_control status 1
Flujo de eventos
DeepStream detecta objeto
  → AnalyticsProcessor.process_detection()
  → Redis: device:{id}:events
  → redis_event_bridge (existente) → Channels → WebSocket → Browser
Para probar
1. Agregá tus modelos entrenados en deepstream-service/config/models/
2. Actualizá deepstream-service/config/deepstream.yml con las RTSP URIs de tus cámaras
3. Corré docker-compose up -d deepstream-service (requiere GPU NVIDIA)
4. Los eventos analytics van al mismo WebSocket que el live view (/ws/device/{id}/)