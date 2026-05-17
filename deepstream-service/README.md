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