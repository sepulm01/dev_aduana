# Notifications

Sistema de notificaciones multi-canal para eventos de analitica de camaras.

## Arquitectura

```
Redis device:*:events
        │
        ▼
notification-bridge (management command)
        │
        ├── Telegram (Bot API, texto + foto + inline keyboard)
        └── Webhook (HTTP POST/GET, JSON)
```

El `notification-bridge` es un servicio Docker que se suscribe a `device:*:events` en Redis, evalua las reglas de notificacion configuradas y envia mensajes por los canales definidos.

## Modelos

### NotificationChannel

Representa un canal de comunicacion (Telegram, Webhook).

| Campo | Tipo | Descripcion |
|---|---|---|
| name | CharField | Nombre descriptivo |
| channel_type | CharField | `telegram` o `webhook` |
| config | JSONField | Configuracion especifica del canal |
| is_active | BooleanField | Habilitar/deshabilitar |

**Config para Telegram:**
```json
{"bot_token": "123:abc", "chat_id": "-456"}
```

**Config para Webhook:**
```json
{"url": "https://hooks.example.com/alert", "method": "POST", "headers": {}, "timeout": 10}
```

### NotificationRule

Define que eventos notificar, por que canal y con que filtros.

| Campo | Tipo | Descripcion |
|---|---|---|
| name | CharField | Nombre de la regla |
| channel | FK | Canal de destino |
| device | FK, nullable | Dispositivo especifico (null = todos) |
| event_codes | JSONField | Codigos de evento: `["DeepStreamDetection", "VideoMotion"]`. Vacio = todos |
| analytics_trigger | JSONField | Tipos de analytics: `["roi", "lc", "oc", "direction"]`. Vacio = sin filtro |
| min_objects | IntegerField | Minimo de objetos detectados (default 0) |
| cooldown_seconds | IntegerField | Tiempo minimo entre notificaciones (default 0) |
| min_duration_seconds | IntegerField | Tiempo minimo de presencia continua antes del primer aviso (default 0) |
| is_active | BooleanField | Habilitar/deshabilitar |
| message_template | TextField | Template de mensaje con variables |
| send_immediate | BooleanField | Envio inmediato sin esperar escalacion |
| send_photo | BooleanField | Adjuntar foto del evento (captura RTSP via ffmpeg) |
| incident_type | FK, nullable | Tipo de incidente asociado para escalacion |

## Como configurar

### 1. Crear un canal (`/notifications/channels/create/`)

**Telegram:**
1. Crear un bot con @BotFather
2. Copiar el token
3. Enviar un mensaje al bot
4. Visitar `https://api.telegram.org/bot<TOKEN>/getUpdates` para obtener el `chat.id`
5. Completar el formulario con nombre, tipo Telegram, token y chat ID

**Webhook:**
1. Completar URL del endpoint que recibira POST con el payload
2. Opcional: headers, metodo HTTP, timeout

### 2. Crear una regla (`/notifications/rules/create/`)

1. Elegir canal y dispositivo (opcional)
2. Configurar filtros: codigos de evento, triggers de analytics, minimo de objetos
3. Configurar tiempos: cooldown y min_duration_seconds
4. Activar envio de foto si se desea
5. Guardar

### 3. El bridge procesa automaticamente

Al crearse la regla, el bridge la cachea en ~30s y comienza a evaluar eventos.

## Variables del template de mensaje

| Variable | Descripcion |
|---|---|
| `{device_name}` | Nombre del dispositivo |
| `{code}` | Codigo del evento (DeepStreamDetection) |
| `{action}` | Accion (Pulse, Start) |
| `{data}` | Datos completos del evento |

## Backends

### TelegramBackend

- `send()` — envia texto via `sendMessage`
- `send_with_reply_markup()` — envia con botones inline (usado por escalacion)
- `send_with_photo()` — envia foto + caption via `sendPhoto` (multipart/form-data)
- `get_updates()` — polling de callback queries (usado por telegram_ack_poller)

### WebhookBackend

- `send()` — POST/PUT JSON a URL configurable
- `send_with_photo()` — incluye la foto como `photo_base64` en el JSON

## Servicios Docker

| Servicio | Comando | Funcion |
|---|---|---|
| `notification-bridge` | `python manage.py notification_bridge` | Escucha Redis, evalua reglas, envia notificaciones |
