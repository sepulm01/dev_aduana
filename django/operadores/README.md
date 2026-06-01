# Operadores

Sistema de gestion de operadores, sites y escalacion basada en perfiles de usuario.

## Arquitectura

```
User (django.contrib.auth)
  └── OperatorProfile (OneToOne)
        ├── escalation_level: 1=respondedor, 2=supervisor, 3=gerente
        ├── personal_channels: Telegram personal, email, etc.
        └── sites (M2M): sites asignados

Site
  ├── channels: canales compartidos (Telegram grupo, etc.)
  └── escalation_levels: SiteEscalationLevel (timeout por nivel)

Device
  └── site (FK): camara asignada a un site
```

## Modelos

### OperatorProfile

Extiende el User de Django con datos del operador.

| Campo | Tipo | Descripcion |
|---|---|---|
| user | OneToOne(User) | Usuario Django |
| phone_number | CharField(20) | Telefono |
| cargo | CharField(120) | Puesto: Guardia, Supervisor, Gerente |
| photo | ImageField | Fotografia |
| escalation_level | IntegerField(default=1) | Nivel fijo: 1=primer respondedor, 2=supervisor, 3=gerente |
| personal_channels | M2M(NotificationChannel) | Canales personales para notificacion individual |
| sites | M2M(Site) | Sites donde opera |

### Site

Agrupacion organizacional con canales y niveles de escalacion.

| Campo | Tipo | Descripcion |
|---|---|---|
| name | CharField(120) | Nombre del site |
| description | TextField | Descripcion |
| channels | M2M(NotificationChannel) | Canales compartidos del site |
| is_active | BooleanField | |

### SiteEscalationLevel

Define los timeouts de escalacion por nivel dentro de un site.

| Campo | Tipo | Descripcion |
|---|---|---|
| site | FK(Site) | Site padre |
| level | IntegerField | Numero de nivel (1, 2, 3...) |
| timeout_seconds | IntegerField | Segundos antes de escalar al siguiente nivel |
| requires_ack | BooleanField | Requiere confirmacion del operador |

### SiteMembership

Registro de pertenencia de un User a un Site.

| Campo | Tipo | Descripcion |
|---|---|---|
| user | FK(User) | Usuario |
| site | FK(Site) | Site |
| is_active | BooleanField | |
| joined_at | DateTimeField | Fecha de asignacion |

## Como funciona la escalacion

1. Un evento en una camara (Device) dispara una regla de notificacion
2. Si la regla tiene `incident_type`, se crea un Incident
3. `incident_manager` (Celery) obtiene el `site` del device
4. Los niveles de escalacion del site definen los timeouts:
   - Level 1: 60s → notifica a canales del site + operadores nivel 1 (canales personales)
   - Level 2: 120s (si no hay ack) → notifica a operadores nivel 2
   - Level 3: ultimo nivel → expira si no hay ack

## Configuracion

### 1. Crear Site (`/operadores/sites/create/`)
- Definir nombre y canales compartidos
- Agregar niveles de escalacion (timeout por nivel, requiere ack)

### 2. Asignar camaras al site
- Al editar un dispositivo, asignarle un site
- O via API: `POST /api/devices/<id>/assign-site/ {"site_id": 1}`

### 3. Configurar perfil del operador (`/operadores/profile/`)
- Telefono, cargo, nivel de escalacion
- Canales personales (Telegram propio)
- El perfil se crea automaticamente al crear el User (signal)

### 4. Asignar operadores a sites (`/operadores/<user_id>/edit/`)
- Seleccionar sites donde opera
- Los canales personales se usan para notificacion individual

## Señales

- `post_save` en User → auto-crea OperatorProfile
