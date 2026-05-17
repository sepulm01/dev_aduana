# Camera Drivers

Abstracción pluggable para comunicar con cámaras IP usando su API nativa (CGI, ONVIF, SDK propietario) en lugar de depender exclusivamente de ONVIF.

## Arquitectura

```
CameraDriver (ABC)
├── DahuaDriver     → CGI nativo (configManager.cgi / eventManager.cgi)
├── HikvisionDriver → CGI nativo + SDK
├── AxisDriver      → VAPIX API
└── OnvifDriver     → Fallback genérico ONVIF Profile S/G/T
```

`get_driver(device)` en `drivers/__init__.py` selecciona el driver apropiado según el campo `manufacturer` del dispositivo.

## Interfaz base (`CameraDriver`)

```python
from onvif_utils.drivers.base import CameraDriver, DriverError

class MiDriver(CameraDriver):
    def detect(self) -> str:
        """Identificador del driver, ej: 'mi_marca'"""

    def get_motion_config(self) -> dict:
        """Lee configuración de detección de movimiento desde la cámara."""

    def set_motion_config(self, config: dict) -> bool:
        """Escribe configuración de detección de movimiento a la cámara."""

    def get_capabilities(self) -> dict:
        """Retorna capacidades del driver/cámara.
        Ej: {"motion_detection": True, "ivs": True, "brand": "mi_marca"}"""

    def poll_motion(self) -> dict | None:
        """Poll de estado de motion. Retorna dict con "motion", "timestamp", "metadata".
        Retorna None si el driver no soporta polling (ver start_event_listener)."""

    def get_ivs_rules(self) -> list[dict]:
        """Lee reglas IVS de la cámara.
        Cada dict incluye: index, name, type, enable, direction, detect_line, detect_region."""

    def set_ivs_rules(self, rules: list[dict]) -> bool:
        """Escribe reglas IVS a la cámara.
        IMPORTANTE: Dahua CGI solo soporta 2 campos por request.
        Campos no soportados deben retornar 'Error' sin lanzar excepción."""

    def get_supported_events(self) -> list[str]:
        """Lista de códigos de eventos que la cámara puede streaming.
        Ej: ["SmartMotionHuman", "CrossLineDetection", "VideoMotion"]"""

    def start_event_listener(callback) -> context:
        """Inicia thread en background escuchando eventos de la cámara.
        El callback recibe dicts: {"code", "action", "index", "data", "timestamp"}
        Retorna un objeto con .cancel() para detener el listener.
        Retorna None si no soporta event listeners."""
```

## Implementación Dahua (`DahuaDriver`)

### Formato CGI

**GET para lectura:**
```
/cgi-bin/configManager.cgi?action=getConfig&name=VideoAnalyseRule
/cgi-bin/configManager.cgi?action=getConfig&name=MotionDetect
```

**GET con query params para escritura (NO POST body):**
```
/cgi-bin/configManager.cgi?action=setConfig&VideoAnalyseRule[0][0].Enable=true
```

Dahua solo soporta **máximo 2 campos por request** en setConfig. Intentar más retorna `Error`.

### Campos IVS known para Dahua

| Campo |Writable?|Notas|
|-------|---------|-----|
|`Enable`|Sí|true/false|
|`Name`|Sí|String|
|`Type`|Sí|CrossLine, CrossRegion|
|`Class`|Sí|Igual a Type en algunos modelos|
|`Direction`|Depende|Some models return Error — skip|
|`DetectLine`|Depende|Coordenadas "x1,y1,x2,y2" — some models reject|
|`DetectRegion`|Depende|Coordenadas "x1,y1...xn,yn" — some models reject|
|`EventHandler`|Depende|Json con LogEnable, RecordEnable, etc — some models reject|

### Requisitos para usar IVS

1. **Habilitar SmartPlan** desde la interfaz web: `Settings > Event > Smart Plan > IVS` (click en el ícono de bombilla)
2. Dibujar las coordenadas de la regla desde la interfaz web si `DetectLine`/`DetectRegion` retornan `Error` por CGI
3. La regla debe estar presente en la cámara (puede crearse manualmente via web) antes de intentar escribirla por API

### Event Stream

El endpoint `eventManager.cgi?action=attach&codes=[All]&heartbeat=30` retorna un stream multipart/x-mixed-replace con eventos en formato:
```
Code=SmartMotionHuman;action=Start;index=0;data={"RegionName":["Region1"],"Object":[{"HumamID":52599,"Rect":[2304,736,2752,2944]}]}
```

## Modelo de datos (`Device`)

El campo `device.manufacturer` determina el driver. Valores soportados:
- `"dahua"`, `"dahu"` → `DahuaDriver`
- cualquier otro → `DahuaDriver` (fallback por ahora)

Para agregar soporte por marca, crear `onvif_utils/drivers/mi_marca.py` con `MiMarcaDriver(CameraDriver)` y actualizar `get_driver()` en `__init__.py`.

## Uso desde views

```python
from onvif_utils.drivers import get_driver
from onvif_utils.drivers.base import DriverError

driver = get_driver(device)

# Leer reglas IVS
rules = driver.get_ivs_rules()

# Escribir reglas IVS (maneja errores graceful)
try:
    driver.set_ivs_rules(rules)
except DriverError as e:
    logger.warning("Camera IVS write failed: %s", e)  # No bloquea

# Habilitar event listener
def on_event(event):
    print(event["code"], event["action"])

ctx = driver.start_event_listener(on_event)
# ... después, para detener:
ctx.cancel()
```

## Errores

Usar `DriverError` para errores de driver (network, auth, camera reject). Nunca lanzar excepciones genéricas.

```python
from onvif_utils.drivers.base import DriverError

raise DriverError(f"Dahua CGI failed: {e}")
```

## Notas para nuevos drivers

1. **Detectar primero** — usar `detect()` para identificar el driver
2. **Graceful degradation** — campos no soportados deben retornar `Error` en setConfig, no lanzar excepción
3. **Thread safety** — `start_event_listener` debe usar thread propio, nunca bloquear
4. **Reconnect** — event listener debe会自动重连 ante desconexiones
5. **Lazy parsing** — no assumes estructura de respuesta, validar antes de acceder