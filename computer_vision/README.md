# Computer Vision — DeepStream Analytics Pipeline

## Resumen

Pipeline de video analítica GPU con DeepStream 8.0 y TensorRT 10.9.
Add/remove dinámico de fuentes RTSP vía Redis pub/sub sin dependencia
de `nvmultiurisrcbin` ni REST API de NVIDIA.

Cada fuente (cámara RTSP, archivo MP4) es un GStreamer bin independiente
con `nvv4l2decoder` (GPU). El `nvstreammux` batcha frames usando pads
dinámicos (`request_pad_simple("sink_N")`). El modelo peoplenet detecta
personas, bolsos y caras. Las detecciones se publican a Redis
`device:{device_id}:events` y se forwardean vía Channels WebSocket al
frontend.

## Arquitectura

```
rtspsrc → h265parse → nvv4l2decoder (Source 0) ─┐
rtspsrc → h265parse → nvv4l2decoder (Source 1) ─┤
filesrc → h264parse → nvv4l2decoder (Source N) ─┤
                                                   ├─→ nvstreammux
                                                       → nvdspreprocess
                                                       → nvinfer(peoplenet)
                                                       → nvtracker
                                                       → nvdsanalytics
                                                       → nvdslogger
                                                          │
                                      [RedisHandler probe] ──→ device:{id}:events (Redis)
                                                          │
                                                       → nvtiler
                                                       → nvvideoconvert
                                                       → nvdsosd
                                                       → fakesink (headless)
```

## Estructura de archivos

```
computer_vision/
├── README.md
├── Dockerfile
├── app/
│   ├── main.cpp                  Entry point
│   ├── pipeline.h / .cpp         Pipeline construccion
│   ├── source_manager.h / .cpp   Add/remove dinamico de fuentes GPU
│   ├── redis_handler.h / .cpp    Redis pub/sub + analytics probe
│   └── Makefile
├── config/
│   ├── pipeline_config.yml       Config del pipeline
│   ├── config_preprocess.txt     Config nvdspreprocess
│   ├── config_tracker_IOU.yml    Tracker
│   ├── config_nvdsanalytics.txt  NVDS analytics
│   └── streammux.txt             Config streammux
└── models/
    └── peoplenet/
        ├── pgie_config.yml
        ├── config_preprocess.txt
        ├── labels.txt
        ├── resnet34_peoplenet_int8.onnx
        └── *.engine
```

## Build & Run

```bash
docker-compose up -d --build computer-vision
docker-compose logs -f computer-vision
```

## Comandos Redis

`RedisHandler` se subscribe a `deepstream:commands`:

```json
{
  "action": "start_preview",
  "device_id": 1,
  "camera_id": "1",
  "rtsp_uri": "rtsp://192.168.1.108:554/cam/realmonitor?channel=1&subtype=0",
  "camera_name": "192.168.1.108",
  "codec": "h265"
}
```

```json
{
  "action": "stop_preview",
  "device_id": 1
}
```

## Flujo de add/remove

### Agregar fuente

1. `redis_handler` recibe `start_preview`
2. `source_manager.add_source(uri, camera_id, device_id, codec)`
3. Construye bin GStreamer segun URI y codec:
   - `rtspsrc → h26[45]depay → h26[45]parse → nvv4l2decoder`
   - `filesrc → qtdemux → h264parse → nvv4l2decoder`
4. `gst_bin_add(pipeline, bin)` + `gst_element_set_state(bin, PLAYING)`
5. Senal `pad-added` del decoder → `nvstreammux.request_pad_simple("sink_N")` → link
6. Mapping: `source_to_device_[N] = device_id` + Redis `deepstream:sources`

### Remover fuente

1. `redis_handler` recibe `stop_preview`
2. `source_manager.remove_source(source_id)`
3. `gst_element_set_state(bin, NULL)`
4. Flush + release pad del streammux
5. `gst_bin_remove(pipeline, bin)`
6. Limpiar memoria + Redis

## Mapeo source_id → device_id

Cada fuente tiene un `source_id` asignado por `SourceManager` (entero secuencial,
no cambia al reconectar). El `RedisHandler` mantiene el mapa en memoria +
Redis hash `deepstream:sources`:

```
deepstream:sources
  0 → "1"          (source_id → device_id)
  0:camera_id → "1"
  0:url → "rtsp://..."
  0:fps → "30"
  1 → "2"
  ...
```

El analytics probe usa este mapa para publicar detecciones al canal
Redis correcto (`device:{device_id}:events`). Fuentes sin mapeo (test
MP4, warmup) se saltan (`device_id < 0 → continue`).

## Como agregar un nuevo modelo

1. Colocar archivos en `models/<nombre>/`:
   - `pgie_config.yml` (config de nvinfer)
   - `config_preprocess.txt` (si usa nvdspreprocess)
   - `model.onnx` o `model.engine`
   - `labels.txt`

2. Actualizar `config/pipeline_config.yml`:
   ```yaml
   primary-gie:
     config-file-path: ../models/<nombre>/pgie_config.yml
   ```

3. Si el modelo usa nvdspreprocess, copiar `config_preprocess.txt`
   al directorio del modelo y actualizar la ruta en `pipeline.cpp:37`.

4. Actualizar `class_labels` en `main.cpp` para que coincida con `labels.txt`.

## Como cambiar codec (H.264 vs H.265)

El `SourceManager` detecta el codec via el parametro `codec` en el comando
`start_preview`. Por defecto usa `h265`:

```json
{"action": "start_preview", ..., "codec": "h264"}
```

Las cadenas GStreamer por codec:
- `h265`: rtspsrc → rtpjitterbuffer → rtph265depay → h265parse → nvv4l2decoder
- `h264`: rtspsrc → rtpjitterbuffer → rtph264depay → h264parse → nvv4l2decoder
- `file`: filesrc → qtdemux → h264parse → nvv4l2decoder

Para agregar nuevos codecs, extender `build_rtsp_bin()` en `source_manager.cpp`.

## Como agregar soporte para fuentes diferentes de RTSP/file

Extender `SourceManager::add_source()` en `source_manager.cpp`:

```cpp
if (uri.rfind("rtsp://", 0) == 0) { ... }
else if (uri.rfind("file://", 0) == 0) { ... }
else if (uri.rfind("webrtc://", 0) == 0) {
    bin = build_webrtc_bin(uri);  // Nuevo metodo
}
```

Implementar `build_webrtc_bin()` con la cadena GStreamer correspondiente.

## Troubleshooting

| Problema | Causa probable | Solucion |
|---|---|---|
| Pipeline no arranca | Config YAML invalida | Verificar `pipeline_config.yml` |
| "One element could not be created" | Falta plugin GStreamer | `gst-inspect-1.0 nvstreammux` etc |
| Sin detecciones | nvdspreprocess sin config | `config_preprocess.txt` debe existir en el path esperado |
| "Could not get sink pad" | batch-size muy chico | Aumentar `batch-size` en `pipeline_config.yml` |
| RTSP no conecta | Credenciales en URI o codec incorrecto | Verificar con ffprobe; ajustar `codec` en start_preview |
| GPU decoder no arranca | nvv4l2decoder no soporta el stream | El codec H.265 debe tener profile compatible |
| Redis no conecta | `redis://redis:6379` inaccesible | Verificar Docker network `docker network ls` |
| FPS cae a 0 post-warmup | Ya no aplica (no hay multiurisrcbin) | Si ocurre, revisar perdida de paquetes RTSP |
