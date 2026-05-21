# DeepStream-Yolo Service

NVIDIA DeepStream 8.0 con DeepStream-Yolo para inferencia YOLOv8 en GPUs NVIDIA.

## Estructura

```
deepstream-service/
├── Dockerfile
├── README.md
└── config/
    ├── deepstream_app_config.txt    # Config de deepstream-app para RTSP
    ├── yolo/
    │   └── config_infer_primary_yoloV8.txt  # Config del modelo YOLO
    └── models/
        ├── yolov8s.onnx              # Modelo ONNX (descargar separately)
        ├── yolov8s.onnx_b1_gpu0.engine  # TensorRT engine (generado en runtime)
        └── labels.txt                # 80 clases COCO
```

## Build

```bash
docker build -f Dockerfile -t deepstream-yolo:latest .
```

## Preparar modelo YOLOv8

1. Descargar el export script:
```bash
git clone https://github.com/marcoslucianops/DeepStream-Yolo.git
cp DeepStream-Yolo/utils/export_yoloV8.py /tmp/
```

2. Descargar modelo YOLOv8 (desde ultralytics):
```bash
wget https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8s.pt
```

3. Convertir a ONNX con dynamic batch:
```bash
python3 export_yoloV8.py -w yolov8s.pt --dynamic --simplify
```

4. Copiar al contenedor o volume:
```bash
cp yolov8s.onnx config/models/
```

## Ejecutar

```bash
# Dentro del contenedor (con GPU)
deepstream-app -c /opt/nvidia/deepstream/config/deepstream_app_config.txt
```

## Docker Compose

Agregar al docker-compose.yml:

```yaml
deepstream-yolo:
  build: ./deepstream-service
  restart: unless-stopped
  environment:
    - DISPLAY=:0
  volumes:
    - ./deepstream-service/config:/opt/nvidia/deepstream/config:ro
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: 1
            capabilities: [gpu]
  depends_on:
    - redis
```

## Notas

- **CUDA 12.8** requerido para DeepStream 8.0
- **TensorRT engine** se genera la primera vez que corre (puede tomar 10+ minutos)
- **RTSP URI** configurable en `deepstream_app_config.txt`
- El contenedor usa `nvcr.io/nvidia/deepstream:8.0-gc-triton-devel` como base



Perfecto. Plan definitivo consolidado:
---
Plan Final — DeepStream C++ con Triton, Dynamic Streams y Redis
Resumen de decisiones
Decisión	Elección
Inference backend	nvinferserver (Triton C-API) para todo
Modelo inicial	YOLOv9 vía Triton, configurable a TrafficCamNet/PeopleNet via app_config.yml
Control de streams	Redis pub/sub deepstream:commands
Output video	fakesink headless + encoder filesink opcional
Mapeo source→device	app_config.yml
Analytics	Solo nvdsanalytics (sin nvdspreprocess)
SGIE	No por ahora, arquitectura preparada para agregar
---
Estructura final de archivos
deepstream-service/
├── Dockerfile                           ← modificar (hiredis, jsoncpp, build C++)
├── app/
│   ├── Makefile
│   ├── deepstream_app.cpp               ← main: pipeline GStreamer + main loop
│   ├── analytics_probe.hpp
│   ├── analytics_probe.cpp              ← pad probe: NvDsAnalytics metadata → Redis
│   ├── redis_publisher.hpp
│   ├── redis_publisher.cpp              ← hiredis wrapper pub/sub
│   ├── stream_manager.hpp
│   └── stream_manager.cpp              ← suscriptor Redis: add/remove sources
└── config/
    ├── app_config.yml                   ← config principal
    ├── config_nvdsanalytics.txt         ← ROIs, líneas, overcrowding
    ├── triton_model_repo/
    │   ├── yolov9/
    │   │   ├── config.pbtxt             ← Triton config para YOLOv9
    │   │   └── 1/                       ← symlink o copy del .engine
    │   ├── Primary_Detector/            ← TrafficCamNet (config ya existe en imagen)
    │   │   └── config.pbtxt
    │   └── peoplenet_transformer/       ← PeopleNet (config ya existe en imagen)
    │       └── config.pbtxt
    └── infer_configs/
        ├── pgie_yolov9.txt              ← nvinferserver config para YOLOv9
        ├── pgie_trafficcamnet.txt       ← nvinferserver config para TrafficCamNet
        └── pgie_peoplenet.txt           ← nvinferserver config para PeopleNet
---
Pipeline GStreamer definitivo
nvmultiurisrcbincreator (≤8 fuentes RTSP, add/remove en runtime)
  → queue1
  → nvinferserver  (PGIE: YOLOv9 | TrafficCamNet | PeopleNet, seleccionado via app_config.yml)
  → queue2
  → nvtracker  (NvDCF + ReID)
  → queue3
  → nvdsanalytics  (ROIs, line-crossing, overcrowding, dirección)
  → queue4  ←── PAD PROBE → RedisPublisher → "device:{id}:events"
  → nvdslogger
  → nvtiler
  → nvvideoconvert
  → nvosd
  → queue5
  → [opcional] nvvideoconvert2 → nvv4l2h264enc → h264parse → filesink
  → fakesink (headless siempre activo)
> El encoder/filesink se habilita con output.video_file: /opt/output/recording.mp4 en app_config.yml. Si la clave no existe, solo fakesink.
---
Configuración Triton para YOLOv9
El único trabajo manual es escribir el config.pbtxt de YOLOv9 con los tensor names del modelo convertido. El nvinferserver config para YOLOv9 usa custom_lib para el parser custom que ya está compilado (libnvdsinfer_custom_impl_Yolo.so):

# config/triton_model_repo/yolov9/config.pbtxt
name: "yolov9"
platform: "tensorrt_plan"
max_batch_size: 8
default_model_filename: "model_b8_gpu0_fp32.engine"
input [{ name: "input", data_type: TYPE_FP32, format: FORMAT_NCHW, dims: [3, 640, 640] }]
output [{ name: "output0", data_type: TYPE_FP32, dims: [84, 8400] }]
instance_group [{ kind: KIND_GPU, count: 1, gpus: [0] }]

# config/infer_configs/pgie_yolov9.txt
infer_config {
  unique_id: 1
  gpu_ids: [0]
  max_batch_size: 8
  backend {
    triton {
      model_name: "yolov9"
      version: -1
      model_repo { root: "/opt/deepstream-config/triton_model_repo" strict_model_config: true }
    }
  }
  preprocess {
    network_format: MEDIA_FORMAT_NONE
    tensor_order: TENSOR_ORDER_LINEAR
    maintain_aspect_ratio: 1
    symmetric_padding: 1
    normalize { scale_factor: 0.00392156862745098 channel_offsets: [0, 0, 0] }
  }
  postprocess { other {} }   # custom parser handles it
  custom_lib { path: "/opt/nvidia/deepstream/deepstream/DeepStream-Yolo/nvdsinfer_custom_impl_Yolo/libnvdsinfer_custom_impl_Yolo.so" }
  extra { custom_process_funcion: "NvDsInferParseYolo" }
}
input_control { process_mode: PROCESS_MODE_FULL_FRAME interval: 1 }
Para TrafficCamNet y PeopleNet, los configs de nvinferserver ya existen en la imagen en samples/configs/deepstream-app-triton/ — solo se copian y adaptan.
---
app_config.yml (estructura completa)
application:
  redis_url: "redis://redis:6379"
  redis_commands_channel: "deepstream:commands"
  redis_events_prefix: "device"
  heartbeat_interval_frames: 300
  perf_log_interval_sec: 5
  output_video_file: ""          # vacío = solo fakesink; "path/to/file.mp4" = grabar
inference:
  pgie_model_name: "yolov9"     # "yolov9" | "trafficcamnet" | "peoplenet"
  # El config file se selecciona automáticamente según pgie_model_name:
  #   yolov9        → infer_configs/pgie_yolov9.txt
  #   trafficcamnet → infer_configs/pgie_trafficcamnet.txt
  #   peoplenet     → infer_configs/pgie_peoplenet.txt
  batch_size: 8
  interval: 1
tracker:
  lib_file: "/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so"
  config_file: "/opt/deepstream-config/config_tracker_NvDCF_accuracy.yml"
  width: 960
  height: 544
analytics:
  enabled: true
  config_file: "/opt/deepstream-config/config_nvdsanalytics.txt"
output:
  tiler_width: 1280
  tiler_height: 720
  osd_enabled: true
sources:
  max_batch_size: 8
  initial:
    - device_id: 5
      sensor_id: "cam_5"
      uri: "rtsp://admin:password@192.168.1.108:554/..."
---
Flujo de datos completo
┌─────────────────────────────────────────────────────────────┐
│  DeepStream C++ App                                         │
│                                                             │
│  main thread:   GStreamer pipeline (nvinferserver/Triton)   │
│  worker thread: Redis subscriber (deepstream:commands)      │
│                                                             │
│  Pad probe en nvdsanalytics:                                │
│    → LineCrossing    → redis.publish("device:5:events", …)  │
│    → ROIOvercrowding → redis.publish("device:5:events", …)  │
│    → Detection       → redis.publish("device:5:events", …)  │
│    → Heartbeat       → redis.publish("device:5:events", …)  │
└─────────────────────────────────────────────────────────────┘
          ↑ add/remove stream             ↓ eventos
   {"action":"add","device_id":5,…}   device:5:events
          ↑                                ↓
┌─────────────────────────────────────────────────────────────┐
│  Redis (ya existente en docker-compose)                     │
│  canal: deepstream:commands         canal: device:*:events  │
└─────────────────────────────────────────────────────────────┘
          ↑                                ↓
┌──────────────────┐              ┌──────────────────────────┐
│  Django view     │              │  redis_event_bridge      │
│  (new endpoint)  │              │  (ya existente, sin       │
│  /api/cameras/   │              │   cambios)               │
│  add_to_ds/      │              └──────────┬───────────────┘
└──────────────────┘                         ↓
                                   Django Channels group
                                   "device_{id}"
                                             ↓
                                   WebSocket → Browser
---
### Archivos que NO requieren cambios
- `redis_event_bridge.py` — ya suscribe a `device:*:events`
- `live/consumers.py` — ya maneja `ivs_event`
- `docker-compose.yml` — solo actualizar el bloque `deepstream-yolo`
---
¿Procedo con la implementación?

1. Construir la imagen:
cd /var/www/dev_security
docker-compose build deepstream-yolo
2. Generar el engine Triton de YOLOv9 (dentro del contenedor):
# La primera vez que corra, Triton necesita el engine.
# El Dockerfile ya linkea el engine existente de worker/:
# /opt/deepstream-config/triton_model_repo/yolov9/1/model_b8_gpu0_fp32.engine
3. Agregar la fuente RTSP real en app_config.yml:
sources:
  initial:
    - device_id: 5
      sensor_id: "cam_5"
      uri: "rtsp://admin:password@192.168.1.108:554/..."
4. Publicar comandos add/remove desde Django (canal deepstream:commands):
import redis
r = redis.from_url("redis://redis:6379")
r.publish("deepstream:commands", json.dumps({
    "action": "add",
    "device_id": 5,
    "sensor_id": "cam_5",
    "uri": "rtsp://..."
}))
5. Cambiar modelo (en app_config.yml):
inference:
  pgie_model_name: "trafficcamnet"  # o "peoplenet"


Resumen completo del flujo:
Cámara RTSP (192.168.1.108)
    ↓
DeepStream Pipeline (nvinfer → nvtracker → nvdsanalytics)
    ↓
analytics_probe.cpp → detecta objetos/ROI/líneas
    ↓
publish_device_event(device_id=5, "DeepStreamDetection", data_json)
    ↓
Redis PubSub → canal "device:5:events"
    ↓
redis_event_bridge (Django) → subscribe "device:*:events"
    ↓
Django Channels → grupo "device_5"
    ↓
WebSocket /ws/device/5/ → navegador

Dónde se almacenan las detecciones:

Paso
Redis
PostgreSQL
WebSocket (navegador)

Formato del mensaje que llega al frontend:

{
  "type": "ivs",
  "device_id": 5,
  "code": "DeepStreamDetection",
  "action": "Detected",
  "timestamp": "2026-05-19T14:50:00Z",
  "data": {
    "source": 0,
    "frame_num": 1234,
    "object_id": 45,
    "class_id": 0,
    "class_label": "auto",
    "confidence": 0.87,
    "bbox": {"left":0.12,"top":0.34,"width":0.05,"height":0.08},
    "device_id": 5
  }
}



20 May 2026:
Implementado. Resumen de lo construido:
Estructura final
deepstream-service/
├── Dockerfile                     # genérico, compila UNA vez
├── bridge/                        # C++ con soporte PGIE + SGIE0 + SGIE1
│   └── deepstream_server_app.cpp  # SGIE se crea solo si hay config
├── models/
│   ├── yolo-v9/                   # ✅ funcional
│   │   ├── config/                # dsserver_config.yml, pgie, preprocess, mux
│   │   ├── labels/labels.txt      # 80 COCO
│   │   └── model_b3_...engine     # 278 MB
│   ├── peoplenet/                 # ✅ configs listos, falta engine
│   └── trafficcamnet-lpd-lpr/     # ✅ configs listos, falta engines
└── scripts/
    ├── entrypoint-model.sh        # symlink + conv etlt→engine + launch
    └── download-models.sh         # guía de descarga NGC
Cómo cambiar de modelo
MODEL=peoplenet docker-compose up -d     # PeopleNet
MODEL=trafficcamnet-lpd-lpr docker-compose up -d  # cadena completa
MODEL=yolo-v9 docker-compose up -d       # default
Lo que falta (para otro momento)
- Descargar engines de NGC para PeopleNet, TrafficCamNet, LPDNet, LPRNet
- El entrypoint convierte .etlt → .engine automáticamente al primer start con GPU


Resumen final
3 de 4 modelos funcionando
Modelo	Pipeline	VRAM	Cambio
YOLOv9	Single PGIE	2.1 GB	MODEL=yolo-v9
PeopleNet	Single PGIE	794 MB	MODEL=peoplenet
TrafficCamNet + LPDNet + LPRNet	PGIE + SGIE0 + SGIE1	980 MB	MODEL=trafficcamnet-lpd-lpr
FaceDetect	❌ Pendiente	—	Solo .etlt, sin versión ONNX
Cómo cambiar de modelo
MODEL=peoplenet docker-compose up -d        # ~5s de switch
MODEL=trafficcamnet-lpd-lpr docker-compose up -d
MODEL=yolo-v9 docker-compose up -d          # default
Estructura final
deepstream-service/
├── Dockerfile                     # único, compila 1 vez
├── bridge/                        # C++ con PGIE + SGIE0 + SGIE1
├── models/
│   ├── yolo-v9/           ✅       # engine pre-compilado
│   ├── peoplenet/         ✅       # ONNX decrypted v2.3.4
│   ├── facenet/           ❌       # .etlt, necesita conversión
│   ├── peoplenet-facedetect/     # config listo, falta FaceDetect engine
│   └── trafficcamnet-lpd-lpr/ ✅  # 3 ONNX: TrafficCamNet + LPDNet + LPRNet
└── scripts/
    ├── entrypoint-model.sh        # symlink active model + launch
    └── download-models.sh         # guía NGC
Lo que resolvió el problema de conversión
Los modelos con sufijo _decrypted o _onnx en NGC vienen en formato ONNX que nvinfer convierte automáticamente con TensorRT 10.9. Los .tlt/.etlt originales usan UFF internamente, deprecado en TRT 10.