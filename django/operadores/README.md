# Operadores

Sistema de gestion de operadores, sites y escalacion basada en perfiles de usuario.

Los niveles de escalacion se definen por Site (SiteEscalationLevel). Cada operador tiene un nivel fijo (escalation_level en OperatorProfile) que determina en que nivel del workflow recibe las alertas.

## Arquitectura

```
User (django.contrib.auth)
  └── OperatorProfile (OneToOne, auto-creado via signal)
        ├── escalation_level: 1=respondedor, 2=supervisor, 3=gerente
        ├── cargo, phone_number, photo
        ├── personal_channels: M2M → NotificationChannel (Telegram personal)
        └── sites: M2M → Site (via SiteMembership)

Site
  ├── name, description, is_active
  ├── channels: M2M → NotificationChannel (canales compartidos)
  └── escalation_levels: SiteEscalationLevel
        ├── level=1, timeout_seconds=60, requires_ack=True
        ├── level=2, timeout_seconds=120, requires_ack=True
        └── level=3, timeout_seconds=0 (ultimo nivel)

Device
  └── site: FK → Site (asignado en /devices/<id>/)
```

## Modelos

### OperatorProfile

Extiende el User de Django. Creado automaticamente via `post_save` signal.

| Campo | Tipo | Descripcion |
|---|---|---|
| user | OneToOne(User) | Usuario Django |
| phone_number | CharField(20) | Telefono |
| cargo | CharField(120) | Puesto/Gerencia |
| photo | ImageField | Fotografia |
| escalation_level | IntegerField(default=1) | Nivel fijo: 1=primer respondedor, 2=supervisor, 3=gerente |
| personal_channels | M2M(NotificationChannel) | Canales personales para notificacion individual |
| sites | M2M(Site) | Sites donde opera |

### Site

Agrupacion organizacional.

| Campo | Tipo | Descripcion |
|---|---|---|
| name | CharField(120) | Nombre del site |
| description | TextField | Descripcion |
| channels | M2M(NotificationChannel) | Canales compartidos (Telegram grupo, etc.) |
| is_active | BooleanField | Habilitar |

### SiteEscalationLevel

Define timeouts de escalacion por nivel dentro de un site.

| Campo | Tipo | Descripcion |
|---|---|---|
| site | FK(Site) | Site padre |
| level | IntegerField | 1, 2, 3... |
| timeout_seconds | IntegerField | Segundos antes de escalar al siguiente nivel |
| requires_ack | BooleanField | Muestra botones [Atender] [Falsa alarma] |

### SiteMembership

Registro de pertenencia User ↔ Site.

| Campo | Tipo | Descripcion |
|---|---|---|
| user | FK(User) | Usuario |
| site | FK(Site) | Site |
| is_active | BooleanField | Habilitado |
| joined_at | DateTimeField | Fecha de alta |

## Paginas

| URL | Vista | Contenido |
|---|---|---|
| `/operadores/sites/` | `site_list` | Lista de sites |
| `/operadores/sites/create/` | `site_create` | Crear site con canales y niveles |
| `/operadores/sites/<id>/edit/` | `site_edit` | Editar site |
| `/operadores/profile/` | `profile_view` | Perfil propio (telefono, cargo, nivel, canales personales, sites) |
| `/operadores/` | `operator_list` | Lista de operadores (admin) |
| `/operadores/<user_id>/edit/` | `operator_edit` | Editar perfil, canales y sites de un operador |
| `/api/devices/<id>/assign-site/` | `device_assign_site` | API para asignar site a una camara |

## Flujo de escalacion

```
Evento en camara → notification_bridge crea Incident
        │
        ▼
incident_manager (Celery 5s)
        │
        ├── device.site → Site
        │
        ├── SiteEscalationLevel L1 (timeout=60s)
        │   ├── Notifica site.channels (Telegram grupo)
        │   └── Notifica operadores con escalation_level=1 via personal_channels
        │
        ├── Timeout 60s sin ack → L2 (timeout=120s)
        │   └── Notifica operadores con escalation_level=2
        │
        └── Timeout total → expired
```

## Configuracion rapida

### 1. Crear Site (`/operadores/sites/create/`)
- Nombre, canales compartidos (checkbox de NotificationChannel)
- Niveles de escalacion con timeout (dinamicos, boton [+agregar])

### 2. Asignar camara al site
En `/devices/<id>/` usar el dropdown de Site, o API:
```
POST /api/devices/<id>/assign-site/  {"site_id": 1}
```

### 3. Perfil del operador (`/operadores/profile/`)
- Telefono, cargo, nivel de escalacion (1/2/3)
- Canales personales (Telegram propio, etc.)
- El perfil se crea automaticamente al crear el User via Django admin

### 4. Asignar operador a sites (`/operadores/<user_id>/edit/`)
- Seleccionar sites donde opera
- Canales personales que recibiran notificaciones individuales

## Senales

- `post_save(User)` → auto-crea `OperatorProfile`
