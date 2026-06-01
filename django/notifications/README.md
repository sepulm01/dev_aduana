# Notifications

Sistema de notificaciones multi-canal para eventos de analitica de camaras.

## Arquitectura

```
Redis device:*:events
        │
        ▼
notification-bridge (management command)
        │
        ├── _check_schedule(rule)         # vigencia + dias + horarios
        ├── _filter_ivs_event(device)     # preset activo
        ├── _rule_matches_event(rule)     # device, codigo, analytics, min_objects
        ├── _check_min_duration(rule)     # umbral de merodeo
        ├── _check_cooldown(rule)         # tiempo entre notificaciones
        │
        ├── _send_notification(rule)      # notificacion inmediata
        │   ├── Telegram (texto + foto via sendPhoto)
        │   └── Webhook (JSON POST)
        │
        └── _create_incident(rule)        # si rule.incident_type != null
            ├── Captura snapshot RTSP
            ├── Guarda Incident + IncidentLog
            └── _broadcast_incident() → WebSocket incident_alert
```

## Modelos

### NotificationChannel

Representa un canal de comunicacion.

| Campo | Tipo | Descripcion |
|---|---|---|
| name | CharField | Nombre descriptivo |
| channel_type | CharField | `telegram` o `webhook` |
| config | JSONField | Configuracion especifica del canal |
| is_active | BooleanField | Habilitar/deshabilitar |

**Config para Telegram:**
```json
{"bot_token": "123:abc", "chat_id": "-456", "parse_mode": "HTML"}
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
| event_codes | JSONField | Codigos de evento. Vacio = todos |
| analytics_trigger | JSONField | `["roi", "lc", "oc", "direction"]`. Vacio = sin filtro |
| min_objects | IntegerField | Minimo de objetos detectados |
| cooldown_seconds | IntegerField | Tiempo minimo entre notificaciones |
| min_duration_seconds | IntegerField | Tiempo de presencia continua antes del primer aviso (merodeo) |
| is_active | BooleanField | Habilitar/deshabilitar |
| message_template | TextField | Template con variables `{device_name}`, `{code}`, `{action}` |
| send_immediate | BooleanField | Envio inmediato sin esperar escalacion |
| send_photo | BooleanField | Adjuntar foto del evento (captura RTSP via ffmpeg) |
| incident_type | FK, nullable | Tipo de incidente asociado para workflow de escalacion |
| valid_from | DateTimeField, nullable | Inicio de vigencia |
| valid_until | DateTimeField, nullable | Fin de vigencia |
| schedule | JSONField | Bloques por dia: `{"mon":[["08:00","18:00"]], "tue":[], ...}` |

## Filtros en orden de evaluacion

En `notification_bridge._handle_event()`, los filtros se evaluan en este orden:

1. **`_check_schedule(rule)`** — early exit. Vigencia, dia de la semana, rango horario
2. **`_filter_ivs_event(device_id, event_data)`** — preset activo (coherente con `redis-event-bridge`)
3. **`_rule_matches_event(rule, device_id, event_data)`** — device, event_codes, analytics_trigger, min_objects
4. **`_check_min_duration(rule, device_id)`** — umbral de merodeo (tracking en memoria)
5. **`_check_cooldown(rule, device_id)`** — cooldown via Redis SETEX

## Como configurar

### 1. Crear un canal (`/notifications/channels/create/`)

**Telegram:**
1. Crear un bot con @BotFather y copiar el token
2. Enviar un mensaje al bot
3. Obtener `chat_id` via `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. Completar formulario: nombre, tipo Telegram, token, chat ID

**Webhook:**
1. URL del endpoint que recibira POST con `{"text": "...", "photo_base64": "..."}`
2. Opcional: metodo HTTP, headers, timeout

### 2. Crear una regla (`/notifications/rules/create/`)

1. Nombre, canal y dispositivo (opcional)
2. Filtros: codigos de evento, triggers de analytics, minimo de objetos
3. Tiempos: cooldown (entre notificaciones) y min_duration_seconds (merodeo)
4. Horario: vigencia, dias con bloques horarios, botones rapidos
5. Foto: checkbox para adjuntar snapshot RTSP
6. Tipo de Incidente: para activar workflow de escalacion
7. Guardar

## Horario programable

La grilla de horario en el formulario permite definir bloques por dia con inputs `time` nativos:

```
Lun  [08:00] → [18:00] [×]  [+ bloque]
Mar  [08:00] → [18:00] [×]  [+ bloque]
...
Sab  (sin bloques) [+ bloque]
Dom  (sin bloques) [+ bloque]

[Lun-Vie 08-18]  [Todo el dia]  [Limpiar]
```

Sin dependencias externas. El `schedule` se serializa como JSON:
```json
{"mon": [["08:00", "18:00"]], "tue": [["22:00", "06:00"]], ...}
```

## Merodeo (min_duration_seconds)

El bridge trackea pares `(rule_id, device_id)` con timestamp de primera deteccion. Si pasan >3s sin deteccion, el contador se reinicia. Solo dispara cuando `now - first_seen >= min_duration_seconds`.

Despues de una notificacion exitosa, el contador se resetea.

## Backends

### TelegramBackend

- `send()` — texto via `sendMessage`
- `send_with_reply_markup()` — texto con botones inline (usado por escalacion)
- `send_with_photo()` — foto JPEG + caption via `sendPhoto` (multipart/form-data)
- `get_updates()` — polling de callback queries

### WebhookBackend

- `send()` — POST/PUT JSON a URL configurable
- `send_with_photo()` — incluye `photo_base64` en el JSON

## Servicios Docker

| Servicio | Comando | Funcion |
|---|---|---|
| `notification-bridge` | `python manage.py notification_bridge` | Suscriptor Redis → evalua reglas → envia notificaciones + crea incidentes |
