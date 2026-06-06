#include "nvdsinfer_custom_impl.h"
#include <cmath>
#include <vector>
#include <string>
#include <cstring>
#include <algorithm>
#include <cstdio>
#include <cstdlib>

static const int NUM_STRIDES = 3;
static const int STRIDES[NUM_STRIDES] = {8, 16, 32};
static const int ANCHORS_PER_LOCATION = 2;

// Estructura para holding información de un stride
struct StrideTensor {
    const float* scores = nullptr;
    const float* bboxes = nullptr;
    int feat_h = 0;
    int feat_w = 0;
    int num_anchors = 0;
};

// Variables estáticas para caching de debug flag
static bool g_debug = false;
static bool g_debug_init = false;

extern "C" bool NvDsInferParseCustomDet10g(
    std::vector<NvDsInferLayerInfo> const& outputLayersInfo,
    NvDsInferNetworkInfo const& networkInfo,
    NvDsInferParseDetectionParams const& detectionParams,
    std::vector<NvDsInferObjectDetectionInfo>& objectList)
{
    // Inicializar debug flag una sola vez
    if (!g_debug_init) {
        g_debug = (std::getenv("DET10G_DEBUG") != nullptr);
        g_debug_init = true;
    }
    
    if (g_debug) {
        fprintf(stderr, "[Det10g] Parser invocado\n");
        fprintf(stderr, "[Det10g] Network: %d x %d\n", networkInfo.width, networkInfo.height);
        fprintf(stderr, "[Det10g] Output layers: %zu\n", outputLayersInfo.size());
    }

    // Inicializar estructuras
    StrideTensor tensors[NUM_STRIDES];
    
    for (int si = 0; si < NUM_STRIDES; ++si) {
        int stride = STRIDES[si];
        int feat_h = (networkInfo.height + stride - 1) / stride;
        int feat_w = (networkInfo.width + stride - 1) / stride;
        tensors[si].feat_h = feat_h;
        tensors[si].feat_w = feat_w;
        tensors[si].num_anchors = feat_h * feat_w * ANCHORS_PER_LOCATION;
        
        if (g_debug) {
            fprintf(stderr, "[Det10g] Stride %d: %d x %d = %d anchors\n",
                    stride, feat_h, feat_w, tensors[si].num_anchors);
        }
    }

    // ONNX output order: 
    // [0] 448: scores stride 8   [12800, 1]
    // [1] 471: scores stride 16  [3200, 1]
    // [2] 494: scores stride 32  [800, 1]
    // [3] 451: bboxes stride 8   [12800, 4]
    // [4] 474: bboxes stride 16  [3200, 4]
    // [5] 497: bboxes stride 32  [800, 4]
    // [6-8]: landmarks (no usados en este parser)
    
    if (outputLayersInfo.size() < 6) {
        fprintf(stderr, "[Det10g] ERROR: Expected at least 6 output tensors, got %zu\n",
                outputLayersInfo.size());
        return false;
    }

    // Matching por índice de salida (orden ONNX)
    tensors[0].scores = static_cast<const float*>(outputLayersInfo[0].buffer);  // 448: stride 8 scores
    tensors[1].scores = static_cast<const float*>(outputLayersInfo[1].buffer);  // 471: stride 16 scores
    tensors[2].scores = static_cast<const float*>(outputLayersInfo[2].buffer);  // 494: stride 32 scores
    
    tensors[0].bboxes = static_cast<const float*>(outputLayersInfo[3].buffer);  // 451: stride 8 bboxes
    tensors[1].bboxes = static_cast<const float*>(outputLayersInfo[4].buffer);  // 474: stride 16 bboxes
    tensors[2].bboxes = static_cast<const float*>(outputLayersInfo[5].buffer);  // 497: stride 32 bboxes
    
    if (g_debug) {
        fprintf(stderr, "[Det10g] Tensors assigned by ONNX output order\n");
        for (int si = 0; si < NUM_STRIDES; ++si) {
            fprintf(stderr, "[Det10g] Stride %d: scores=%p bboxes=%p\n",
                    STRIDES[si], tensors[si].scores, tensors[si].bboxes);
        }
    }

    float conf_threshold = 0.5f;
    if (!detectionParams.perClassPreclusterThreshold.empty()) {
        conf_threshold = detectionParams.perClassPreclusterThreshold[0];
    }

    const float input_w = static_cast<float>(networkInfo.width);
    const float input_h = static_cast<float>(networkInfo.height);

    int total_detections = 0;

    for (int si = 0; si < NUM_STRIDES; ++si) {
        const StrideTensor& t = tensors[si];
        int stride = STRIDES[si];
        int feat_h = t.feat_h;
        int feat_w = t.feat_w;
        int num_anchors = t.num_anchors;
        int stride_detections = 0;

        if (!t.scores || !t.bboxes) {
            fprintf(stderr, "[Det10g] ERROR: Null tensor pointers for stride %d\n", stride);
            continue;
        }

        for (int y = 0; y < feat_h; ++y) {
            for (int x = 0; x < feat_w; ++x) {
                float cx = (x + 0.5f) * stride;
                float cy = (y + 0.5f) * stride;

                for (int a = 0; a < ANCHORS_PER_LOCATION; ++a) {
                    int idx = (y * feat_w + x) * ANCHORS_PER_LOCATION + a;
                    if (idx >= num_anchors) {
                        if (g_debug) {
                            fprintf(stderr, "[Det10g] Index out of bounds: idx=%d >= %d\n", idx, num_anchors);
                        }
                        continue;
                    }

                    float score = t.scores[idx];
                    if (score < conf_threshold) continue;

                    int bbox_base = idx * 4;
                    if (bbox_base + 3 >= num_anchors * 4) {
                        if (g_debug) {
                            fprintf(stderr, "[Det10g] Bbox out of bounds: base=%d\n", bbox_base);
                        }
                        continue;
                    }

                    // Decodificar bbox: FCOS format (l, t, r, b offsets from center)
                    float l = t.bboxes[bbox_base + 0] * stride;
                    float top = t.bboxes[bbox_base + 1] * stride;
                    float r = t.bboxes[bbox_base + 2] * stride;
                    float bottom = t.bboxes[bbox_base + 3] * stride;

                    float x1 = cx - l;
                    float y1 = cy - top;
                    float x2 = cx + r;
                    float y2 = cy + bottom;

                    // Clip a límites de imagen
                    x1 = std::max(0.0f, std::min(x1, input_w));
                    y1 = std::max(0.0f, std::min(y1, input_h));
                    x2 = std::max(0.0f, std::min(x2, input_w));
                    y2 = std::max(0.0f, std::min(y2, input_h));

                    if (x2 <= x1 || y2 <= y1) continue;

                    // CRÍTICO: DeepStream espera píxeles absolutos, NO normalizados
                    NvDsInferObjectDetectionInfo det;
                    det.classId = 0;
                    det.detectionConfidence = score;
                    det.left = x1;           // píxeles, no normalizado
                    det.top = y1;            // píxeles, no normalizado
                    det.width = x2 - x1;     // píxeles, no normalizado
                    det.height = y2 - y1;    // píxeles, no normalizado

                    objectList.push_back(det);
                    stride_detections++;
                    total_detections++;
                }
            }
        }

        if (g_debug) {
            fprintf(stderr, "[Det10g] Stride %d: %d detections\n", stride, stride_detections);
        }
    }

    if (g_debug) {
        fprintf(stderr, "[Det10g] Total detections: %d\n", total_detections);
    }

    return true;
}

CHECK_CUSTOM_PARSE_FUNC_PROTOTYPE(NvDsInferParseCustomDet10g);