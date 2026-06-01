# Incidents

Sistema de gestion de incidentes con escalacion por niveles y acknowledgment.

## Arquitectura

```
notification_bridge
        │
        ├── Envia notificacion inmediata
        └── Crea Incident (si la regla tiene IncidentType)
                │
                ▼
        incident_manager (Celery cada 5s)
                │
                ├── Level 1: notifica + espera ack
                │   ├── Ack → resolved
                │   └── Timeout → Level 2
                ├── Level 2: notifica grupo superior
                │   ├── Ack → resolved
                │   └── Timeout → Level 3 (o expired)
                └── Auto-resolve: cierra tras X segundos

telegram_ack_poller
        │
        Polling getUpdates cada 2s
        │
        └── Detecta botones ["Atender", "Falsa alarma"] → ack/resolve
```

## Modelos

### IncidentType

Plantilla de workflow de escalacion.

| Campo | Tipo | Descripcion |
|---|---|---|
| name | CharField | Nombre (ej: "Intrusion ROI", "Dispositivo Offline") |
| description | TextField | Descripcion del tipo de incidente |
| is_active | BooleanField | Habilitar/deshabilitar |
| auto_resolve_seconds | IntegerField | Cerrar automaticamente tras N segundos (0 = no) |
| dedup_window_seconds | IntegerField | No crear nuevo incidente si ya hay uno activo en N segundos |

### EscalationLevel

Nivel de escalacion dentro de un IncidentType.

| Campo | Tipo | Descripcion |
|---|---|---|
| incident_type | FK | Tipo de incidente padre |
| level | IntegerField | Numero de nivel (1, 2, 3...) |
| channel | FK | Canal de notificacion para este nivel |
| timeout_seconds | IntegerField | Tiempo de espera antes de escalar al siguiente nivel |
| requires_ack | BooleanField | Requiere confirmacion del usuario (muestra botones) |
| message_template | TextField | Template opcional para el mensaje |
| auto_actions | JSONField | Acciones automaticas (futuro) |

### Incident

Instancia de un incidente activo.

| Campo | Tipo | Descripcion |
|---|---|---|
| incident_type | FK | Tipo de incidente |
| device | FK | Dispositivo donde ocurrio |
| rule | FK, nullable | Regla de notificacion que lo disparo |
| event_data | JSONField | Datos del evento original |
| status | CharField | `active`, `acknowledged`, `resolved`, `expired` |
| current_level | IntegerField | Nivel actual de escalacion |
| acknowledged_by | CharField | Quien atendio (username Telegram o "api") |
| acknowledged_at | DateTimeField | Cuando se atendio |
| level_started_at | DateTimeField | Cuando empezo el nivel actual |
| created_at | DateTimeField | Cuando se creo el incidente |
| resolved_at | DateTimeField | Cuando se resolvio |

### IncidentLog

Bitacora de acciones sobre un incidente.

| Campo | Tipo | Descripcion |
|---|---|---|
| incident | FK | Incidente |
| level | IntegerField | Nivel al que corresponde la accion |
| action | CharField | `created`, `notified`, `escalated`, `acknowledged`, `resolved`, `expired` |
| success | BooleanField | Si la accion fue exitosa |
| detail | JSONField | Detalles adicionales |
| timestamp | DateTimeField | Cuando ocurrio |

## Como configurar

### 1. Crear un IncidentType (`/incidents/types/create/`)

1. Definir nombre y descripcion
2. Configurar auto-resolve y dedup window (opcional)
3. Agregar niveles de escalacion:
   - **Nivel 1**: canal Telegram, 60s timeout, requiere ack
   - **Nivel 2**: canal Telegram grupo, 120s timeout, requiere ack
   - **Nivel 3**: canal Webhook, sin ack requerido

### 2. Asociar a una NotificationRule

Al crear/editar una regla de notificacion, opcionalmente asociar un `IncidentType`. Cuando la regla se dispara, ademas de la notificacion inmediata, se crea un incidente con el workflow de escalacion.

### 3. El incident_manager procesa automaticamente

Cada 5s (Celery Beat), evalua incidentes activos:
- Envia notificaciones de nivel si no se han enviado
- Verifica timeouts y escala si es necesario
- Cierra incidentes que excedieron `auto_resolve_seconds`

### 4. Acknowledgment via Telegram

El `telegram_ack_poller` hace polling de `getUpdates` cada 2s. Cuando un usuario presiona "Atender" o "Falsa alarma" en un mensaje de escalacion:
- "Atender" → incidente marked as `acknowledged`, escalacion detenida
- "Falsa alarma" → incidente marked as `resolved`

Tambien existe un endpoint REST: `POST /api/incidents/{id}/ack/` con `{"by": "nombre"}`.

## Servicios Docker

| Servicio | Comando | Funcion |
|---|---|---|
| `telegram-ack-poller` | `python manage.py telegram_ack_poller` | Polling de callbacks de Telegram |
| `celery-beat` | `celery -A config beat` | Dispara `incident_manager` cada 5s |
| `celery-worker` | `celery -A config worker` | Ejecuta `incident_manager` |

## Maquina de estados

```
[evento] → active (level=1)
              ├── ack → acknowledged → resolved
              ├── timeout → active (level=2)
              │               ├── ack → acknowledged → resolved
              │               └── timeout → active (level=3)
              │                               ├── ack → acknowledged → resolved
              │                               └── sin mas niveles → expired
              └── auto_resolve → resolved
```
