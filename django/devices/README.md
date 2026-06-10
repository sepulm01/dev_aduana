# Devices App

## Multi-instance DeepStream Pipelines

### Overview

Soporta hasta `MAX_INSTANCES = 4` contenedores por pipeline (16 total en docker-compose).
Los dispositivos se distribuyen round-robin entre las instancias disponibles.
Las instancias sin dispositivos se apagan automaticamente (on-demand).

### Configuracion por pipeline

| Pipeline | `max_streammux_batch` | `max_devices_per_instance` | Engine max batch |
|----------|----------------------|---------------------------|-----------------|
| main | 3 | 3 | `_b3_` |
| retinaface | 1 | 1 | 1 (cableado en ONNX) |
| yolov9 | 3 | 3 | `_b3_` |
| trafficcamnet_lpr | 1 | 1 | `_b1_` |

### Distribucion de dispositivos

Ejemplo: 2 camaras en retinaface con `max_devices_per_instance=1`:

```
instances_needed = ceil(2 / 1) = 2

retinaface-1 → camara 109 (config_retinaface.yml)
retinaface-2 → camara 108 (config_retinaface_2.yml)
retinaface-3 → empty (stopped)
retinaface-4 → empty (stopped)
```

### Config files per instance

| Instancia | Config | Redis sources key |
|-----------|--------|-------------------|
| 1 | `config_{pipeline}.yml` | `deepstream:sources:{pipeline}:1` |
| N (2-4) | `config_{pipeline}_{N}.yml` | `deepstream:sources:{pipeline}:{N}` |

### Container naming

| Pipeline | Instance 1 | Instance 2 |
|----------|-----------|-----------|
| main | `mediamtx-manager-computer-vision-1` | `computer-vision-2-1` |
| retinaface | `-retinaface-1` | `-retinaface-2-1` |
| yolov9 | `-yolov9-1` | `-yolov9-2-1` |
| lpr | `-lpr-1` | `-lpr-2-1` |

### Escalamiento a 100 camaras

- Batch = 1 pipelines (retinaface, lpr): ~100 contenedores si `max_devices_per_instance=1`
- Batch > 1 pipelines (main, yolov9): ~13 contenedores con batch=8 tras recompilar ONNX
- Ruta de escalamiento: re-exportar ONNX con batch dinamico → Swarm/K8s multi-host

### Batch-size protection (doble capa)

1. **Django** (`config_generator.py`): `max_streammux_batch` capea el streammux al max batch del engine
2. **C++** (`pipeline_test3.cpp:912`): `MIN(pgie_batch_size, num_sources)` capea el PGIE en runtime
Ambas capas deben coincidir para evitar segfaults de TensorRT.

---

# Patrullaje PTZ

## Overview

Sistema de patrullaje automatico para camaras PTZ. Cada dispositivo puede tener multiples patrullas (`Patrol`) que definen un recorrido entre presets ONVIF con horario semanal opcional. Un controlador Celery ejecuta el movimiento cada 10s.

## Arquitectura

```
celery-beat (cada 10s)
  +-- patrol_controller
        +-- Evalua schedule (is_active, valid_from/until, weekly blocks)
        +-- Si activo: avanza al siguiente preset del preset_order
        +-- Llama ONVIF goto_preset(token, speed)
        +-- Espera dwell_seconds via Redis timestamp
        +-- Lock Redis por dispositivo (solo 1 patrol activa a la vez)
```

## Model

### Patrol

| Field | Type | Notes |
|-------|------|-------|
| device | FK(Device) | CASCADE, related_name="patrols" |
| name | CharField(120) | Nombre del patrullaje |
| is_active | BooleanField | Master toggle |
| valid_from | DateTimeField(null) | Inicio de vigencia |
| valid_until | DateTimeField(null) | Fin de vigencia |
| schedule | JSONField | `{"mon": [["HH:MM","HH:MM"]], ...}` |
| dwell_seconds | IntegerField | Pausa en cada preset (default 10) |
| speed | FloatField | Velocidad PTZ (0.25-1.0, default 1.0) |
| preset_order | JSONField | `["token1", "token2", "token3"]` |
| created_at | DateTimeField | auto_now_add |
| updated_at | DateTimeField | auto_now |

Migracion: `devices/0017_patrol`

## Schedule format

Identico al de `NotificationRule.schedule`:

```json
{
  "mon": [["08:00", "12:00"], ["14:00", "18:00"]],
  "tue": [["08:00", "18:00"]],
  "wed": [],
  ...
}
```

Llaves: `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, `sun`. Cada bloque es un array `[inicio, fin]` en formato `"HH:MM"` 24h. Evaluacion: `inicio <= ahora < fin`. Horario vacio = sin restriccion horaria.

## Redis keys

| Key | Purpose | TTL |
|-----|---------|-----|
| `patrol:{id}:index` | Current preset index in preset_order | none |
| `patrol:{id}:next_move` | Unix timestamp when next move allowed | dwell+60s |
| `patrol:lock:{device_id}` | Per-device patrol lock (only 1 active) | 15s |
| `patrol:{device_id}:moving` | Flag when camera busy (manual control) | 30s |

## Celery Beat schedule

```python
"patrol-controller-every-10s": {
    "task": "devices.tasks.patrol_controller",
    "schedule": 10.0,
}
```

## URL endpoints

| URL | View | Method | Purpose |
|-----|------|--------|---------|
| `/devices/<id>/patrols/` | patrol_list | GET | Lista de patrullas del dispositivo |
| `/devices/<id>/patrols/add/` | patrol_form | GET | Formulario nuevo patrullaje |
| `/devices/<id>/patrols/edit/<pid>/` | patrol_form | GET | Formulario editar patrullaje |
| `/api/devices/<id>/patrols/save/` | patrol_save | POST | Crear patrullaje |
| `/api/devices/<id>/patrols/save/<pid>/` | patrol_save | POST | Actualizar patrullaje |
| `/api/devices/<id>/patrols/delete/<pid>/` | patrol_delete | POST | Eliminar patrullaje |
| `/api/devices/<id>/patrols/toggle/<pid>/` | patrol_toggle | POST | Activar/desactivar |

## UI flow

1. **Acceso**: pagina de detalle del dispositivo > card "Patrullaje PTZ" (solo si `camera_specs.ptz_caps`)
2. **Lista**: `/devices/<id>/patrols/` muestra todas las patrullas con nombre, estado, horario resumido
3. **Formulario**: nombre, toggle activo, dwell time, velocidad, fechas, grilla horaria semanal, selector de presets
4. **Presets**: se consultan via ONVIF al cargar el form. Click `+` para agregar al recorrido, `×` para quitar. El orden en la lista es el orden del recorrido.

## Patrol controller logic

1. Itera `Patrol.objects.filter(is_active=True)`
2. Si `preset_order` vacio o `device` no tiene PTZ → skip
3. Evalua horario via `_patrol_in_schedule()`. Si fuera de horario → resetea indice
4. Adquiere lock Redis por dispositivo (`patrol:lock:{device_id}`)
5. Si camara esta en movimiento (manual) → skip
6. Si `dwell_seconds` no ha pasado → skip
7. Ejecuta `goto_preset(profile_token, preset_token, speed)`
8. Avanza indice, setea `next_move` timestamp

## Requirements

- Camara con soporte PTZ ONVIF (`camera_specs.ptz_caps` truthy)
- Al menos un preset guardado en la camara (via pagina Live)
- Redis para locks y estado
- Celery worker con acceso a la red de la camara
