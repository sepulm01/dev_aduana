# Incidents

Sistema de gestion de incidentes con escalacion por operadores y acknowledgment via Telegram.

## Arquitectura

```
notification_bridge._create_incident()
        │
        ├── Captura snapshot RTSP (ImageField)
        ├── Crea Incident + IncidentLog
        └── _broadcast_incident() → WebSocket global
                │
                ▼
        incident_manager (Celery cada 5s)
                │
                ├── Obtiene site del device
                ├── SiteEscalationLevel (timeout por nivel)
                │
                ├── Level 1: notifica site channels + operadores nivel 1
                │   ├── Ack → acknowledged → resolved
                │   └── Timeout → Level 2
                │
                ├── Level 2: notifica site channels + operadores nivel 2
                │   ├── Ack → resolved
                │   └── Timeout → Level N (o expired)
                │
                └── Auto-resolve: cierra tras N segundos

telegram_ack_poller (polling getUpdates cada 2s)
        │
        └── Detecta botones [Atender alerta] [Falsa alarma]
            ├── "ack_N" → incident.status = "acknowledged"
            └── "false_N" → incident.status = "resolved"

WebSocket /ws/incidents/
        │
        └── Toast en cualquier pagina al crearse un incidente
```

## Modelos

### IncidentType

Plantilla de incidente (sin niveles — los niveles estan en Site).

| Campo | Tipo | Descripcion |
|---|---|---|
| name | CharField | Nombre (ej: "Intrusion ROI") |
| description | TextField | Descripcion |
| is_active | BooleanField | Habilitar/deshabilitar |
| auto_resolve_seconds | IntegerField | Cerrar automaticamente tras N segundos (0 = no) |
| dedup_window_seconds | IntegerField | No crear duplicado si ya hay uno activo en N segundos |

### Incident

Instancia de un incidente.

| Campo | Tipo | Descripcion |
|---|---|---|
| incident_type | FK | Tipo de incidente |
| device | FK | Dispositivo donde ocurrio |
| rule | FK, nullable | Regla que lo disparo |
| event_data | JSONField | Datos completos del evento original |
| status | CharField | `active` → `acknowledged` → `resolved` \| `expired` |
| current_level | IntegerField | Nivel actual de escalacion |
| acknowledged_by | CharField | Quien atendio (username) |
| acknowledged_at | DateTimeField | Cuando se atendio |
| level_started_at | DateTimeField | Cuando empezo el nivel actual |
| created_at | DateTimeField | Creacion |
| resolved_at | DateTimeField | Resolucion |
| snapshot | ImageField | Foto JPEG del momento del incidente |

### IncidentLog

Bitacora de auditoria.

| Campo | Tipo | Descripcion |
|---|---|---|
| incident | FK | Incidente |
| level | IntegerField | Nivel |
| action | CharField | `created`, `notified`, `notified_user`, `escalated`, `acknowledged`, `resolved`, `expired` |
| success | BooleanField | Exito de la accion |
| detail | JSONField | Metadatos (channel_name, user, reason, etc.) |
| timestamp | DateTimeField | Cuando ocurrio |

## Paginas

| URL | Vista | Contenido |
|---|---|---|
| `/incidents/` | `incident_list` | Lista de incidentes con icono detalle + boton atender |
| `/incidents/<id>/` | `incident_detail` | Detalle completo: datos, objetos, analytics, foto, bitacora |
| `/incidents/dashboard/` | `incident_dashboard` | Incidentes activos con iframe WebRTC de la camara |
| `/incidents/types/` | `incident_type_list` | CRUD de tipos de incidente |
| `/api/incidents/<id>/ack/` | `incident_ack` | API REST para acknowledgment |

## Como configurar

### 1. Crear IncidentType (`/incidents/types/create/`)
Definir nombre, auto_resolve_seconds y dedup_window_seconds.

### 2. Vincular a NotificationRule
En `/notifications/rules/<id>/edit/`, seleccionar el IncidentType en el dropdown.

### 3. Asignar Device a Site
En `/devices/<id>/`, seleccionar el site en el dropdown de Informacion.

### 4. Configurar Site con niveles
En `/operadores/sites/<id>/edit/`, definir niveles con timeouts.

## Maquina de estados

```
[evento detectado]
       │
       ▼
   active (L=1) ──notifica site channels + operadores nivel 1──
       │                                                        │
       ├── timeout sin ack                                      ├── ack → acknowledged → resolved
       │     │                                                  │
       │     ▼                                                  │
       │   active (L=2) ──notifica nivel 2──                    │
       │     │                              │                    │
       │     ├── timeout                    ├── ack              │
       │     │     │                        │                    │
       │     │     ▼                        │                    │
       │     │   expired (sin mas niveles)   │                    │
       │     │                              │                    │
       │     └── auto_resolve → resolved     │                    │
       │                                     │                    │
       └── auto_resolve → resolved (si elapsed >= auto_resolve_seconds)
```

## Servicios Docker

| Servicio | Comando | Funcion |
|---|---|---|
| `notification-bridge` | `python manage.py notification_bridge` | Crea Incident al detectar evento |
| `celery-beat` | `celery -A config beat` | Dispara `incident_manager` cada 5s |
| `celery-worker` | `celery -A config worker` | Ejecuta `incident_manager` |
| `telegram-ack-poller` | `python manage.py telegram_ack_poller` | Polling de callbacks Telegram |
