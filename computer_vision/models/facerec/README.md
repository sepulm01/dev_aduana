# Face Recognition Pipeline — Límites y Umbrales

Una cara detectada por este pipeline solo se guarda en la base de datos si pasa **todos** los filtros en cadena.

Cada filtro corresponde a un archivo y línea específica del código.

---

## Cadena de filtros

```
Cámara/MP4 → Peoplenet PGIE → C++ Probe → TCP → FaceBuffer → Matching → DB
                │                │                   │           │
           ┌────┘           ┌────┘              ┌────┘      ┌────┘
     pgie_config.yml   pipeline_test3.cpp   face_receiver.py  face_receiver.py
```

---

## 1. Filtros en el PGIE (Peoplenet)

**Archivo:** `computer_vision/models/facerec/pgie_config.yml`

| Límite | Dónde | Valor | Efecto |
|---|---|---|---|
| `pre-cluster-threshold` | `class-attrs-all` línea 36 | **0.4** | Confianza mínima para cualquier clase |
| `pre-cluster-threshold` | `class-attrs-2` línea 41 | **0.55** | Confianza mínima específica para Face (class_id=2) |
| `nms-iou-threshold` | `class-attrs-all` línea 37 | **0.4** | IOU máximo para suprimir detecciones duplicadas |
| `nms-iou-threshold` | `class-attrs-2` línea 42 | **0.35** | IOU máximo para suprimr duplicados de Face |
| `topk` | `class-attrs-all` línea 38 | **100** | Máximo detecciones reportadas por frame |
| `batch-size` | `property` | **3** | Batch de inferencia GPU |
| `network-mode` | `property` | **0** | Precisión FP32 |
| `infer-dims` | `property` | **3;544;960** | Dimensiones de entrada al modelo |

---

## 2. Filtros en el Probe C++

**Archivo:** `computer_vision/app/pipeline_test3.cpp`

| Límite | Línea | Constante | Valor | Efecto |
|---|---|---|---|---|
| Tamaño mínimo de bbox | 557, 37 | `FACE_MIN_BBOX_PX` | **30 px** | Caras < 30×30 px ignoradas |
| Tracker ID inválido | 553 | — | `object_id == 0` o `UNTRACKED_OBJECT_ID` | Objetos no rastreados ignorados |
| Clase | 552 | — | `class_id != 2` | Solo caras (Peoplenet class_id=2) |
| Quality gate — ojo izquierdo | 175, 53 | `KEY_REGIONS[0]` = `{68, 75, 1}` | ≥1 punto en [0,1] | Región 68-75 con al menos 1 punto visible |
| Quality gate — ojo derecho | 175, 53 | `KEY_REGIONS[1]` = `{76, 83, 1}` | ≥1 punto en [0,1] | Región 76-83 con al menos 1 punto visible |
| Quality gate — nariz | 175, 53 | `KEY_REGIONS[2]` = `{53, 67, 1}` | ≥1 punto en [0,1] | Región 53-67 con al menos 1 punto visible |
| Quality gate — boca | 175, 53 | `KEY_REGIONS[3]` = `{84, 95, 2}` | ≥2 puntos en [0,1] | Región 84-95 con ambas comisuras visibles |
| Quality score final | 578 | — | `quality == 1.0` | Los 4 checks anteriores deben pasar todos |
| Cooldown de envío | 37 | `SNAP_COOLDOWN_US` | **3 segundos** | Mínimo entre envíos consecutivos de crops |

### Detalle del quality gate

El modelo `2d106det` (SGIE0) produce 106 landmarks faciales. El C++ verifica 4 regiones anatómicas:

| Región | Rango landmarks | Mínimo puntos | Significado |
|---|---|---|---|
| Ojo izquierdo | 68–75 | 1 | Al menos 1 punto del ojo en [0,1] |
| Ojo derecho | 76–83 | 1 | Al menos 1 punto del ojo en [0,1] |
| Nariz | 53–67 | 1 | Al menos 1 punto de la nariz en [0,1] |
| Boca exterior | 84–95 | 2 | Ambas comisuras en [0,1] |

Si **cualquiera** de las 4 regiones no cumple su mínimo, `quality = 0.0` y el frame se descarta.

---

## 3. Filtros en el FaceBuffer (Python)

**Archivo:** `django/live/management/commands/face_receiver.py`

| Límite | Línea | Constante | Valor | Efecto |
|---|---|---|---|---|
| Timeout de buffer | 25 | `BUFFER_TIMEOUT_SEC` | **10 s** | El objeto debe desaparecer 10s para guardarse |
| Score mínimo | 26 | `SCORE_THRESHOLD` | **0.0** | Sin mínimo (acepta todo) |
| Score fórmula | 51 | — | `quality × (1.0 + area × 0.1)` | Prioriza calidad sobre área |
| Mejor score por objeto | 58-62 | — | Solo el mejor score por `(device_id, object_id)` | Deduplicación intra-frame |

---

## 4. Filtros en el guardado a DB (Python)

**Archivo:** `django/live/management/commands/face_receiver.py`

| Límite | Línea | Valor | Efecto |
|---|---|---|---|
| Calidad de imagen (Laplacian) | 206-212 | `LAPLACIAN_THRESHOLD` = **50** | Crop borroso → descartado. `cv2.Laplacian(gray, CV_64F).var()`. <50 = blur, 50-150 = okay, >150 = sharp |
| Embedding todo-ceros | 222-225 | `abs(v) < 1e-10` | Si embedding es cero → `None`, no se hace matching |
| Landmarks todo-ceros | 227-230 | `abs(v) < 1e-10` | Si landmarks son cero → `None` |
| Cooldown de matching | 239 | `COOLDOWN_SEC` = **10 s** | Misma persona no se guarda 2 veces en 10s |
| Cosine distance | 289 | `< 0.35` | Distancia coseno máxima para considerar un match |

**Archivo:** `django/config/settings.py`

| Límite | Línea | Valor | Efecto |
|---|---|---|---|
| Cooldown global | 125 | `FACE_MATCH_COOLDOWN_SECONDS = 10` | Tiempo mínimo entre saves de la misma persona |
| QA Laplacian threshold | 127 | `FACE_QUALITY_LAPLACIAN_THRESHOLD = 50` | Umbral de varianza Laplaciana para crops |

---

## 5. Conexiones TCP

| Puerto | Receptor | Propósito |
|---|---|---|
| **12348** | `face-receiver` | Crops faciales + embeddings + landmarks |
| **12349** | `snapshot-receiver` | Snapshots de analytics (ROI/LC/OC) |

---

## 6. Modelos en este directorio

| Archivo | Rol | Pipeline |
|---|---|---|
| `resnet34_peoplenet_int8.onnx` | PGIE — Detección de personas/rostros | Facerec |
| `resnet34_peoplenet_int8.onnx_b3_gpu0_fp32.engine` | PGIE — Engine TensorRT compilado | Facerec |
| `resnet34_peoplenet_int8.txt` | PGIE — Cache de calibración INT8 | Facerec |
| `config_preprocess.txt` | PGIE — Preprocesamiento nvdspreprocess | Facerec |
| `labels.txt` | PGIE — Etiquetas: Person, Bag, Face | Facerec |
| `pgie_config.yml` | PGIE — Configuración de inferencia | Facerec |
| `2d106det.onnx` | SGIE0 — 106 landmarks faciales | Facerec |
| `2d106det.onnx_b3_gpu0_fp32.engine` | SGIE0 — Engine TensorRT | Facerec |
| `sgie0_config.yml` | SGIE0 — Config (`operate-on-class-ids: 2`) | Facerec |
| `w600k_r50.onnx` | SGIE1 — ArcFace 512-d embedding | Facerec |
| `w600k_r50.onnx_b3_gpu0_fp32.engine` | SGIE1 — Engine TensorRT | Facerec |
| `sgie1_config.yml` | SGIE1 — Config (`operate-on-class-ids: 2`) | Facerec |
| `det_10g.onnx` | Legacy — RetinaFace (no usado actualmente) | — |
| `1k3d68.onnx` | Legacy | — |
| `genderage.onnx` | Legacy | — |
| `peoplenet.onnx` | Legacy — Versión antigua de Peoplenet | — |
| `peoplenet.engine` | Legacy — Engine antiguo | — |
| `labels_people.txt` | Legacy | — |

---

## Resumen visual

```
Frame de video
 │
 ├─ Bbox < 30px? ──────────── DESCARTAR
 ├─ class_id != 2? ────────── DESCARTAR
 ├─ object_id == 0? ───────── DESCARTAR
 │
 ├─ Peoplenet confidence < 0.55? ── DESCARTAR
 │
 ├─ SGIE0: 4 regiones anatómicas OK?
 │   ├─ Ojo izq: ≥1 punto en [0,1]
 │   ├─ Ojo der: ≥1 punto en [0,1]
 │   ├─ Nariz: ≥1 punto en [0,1]
 │   └─ Boca: ≥2 puntos en [0,1]
 │   └─ Alguna falla? ──────── DESCARTAR
 │
 ├─ SGIE1: embedding todo ceros? ── Sin matching
 │
 ├─ Crop blurry? (Laplacian < 50) ──── DESCARTAR
 │
 ├─ Cooldown 10s de misma persona? ── DESCARTAR
 │
 └─ GUARDAR en DB → Detection
```
