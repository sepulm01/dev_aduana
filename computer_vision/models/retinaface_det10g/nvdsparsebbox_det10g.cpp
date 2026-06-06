#include "nvdsinfer_custom_impl.h"
#include <cmath>
#include <vector>
#include <string>
#include <cstring>
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <mutex>

static const int NUM_STRIDES = 3;
static const int STRIDES[NUM_STRIDES] = {8, 16, 32};
static const int ANCHORS_PER_LOCATION = 2;
static const int NUM_LANDMARKS = 5;

struct StrideTensor {
    const float* scores = nullptr;
    const float* bboxes = nullptr;
    const float* kps    = nullptr;
    int feat_h = 0;
    int feat_w = 0;
    int num_anchors = 0;
};

// Landmarks de la última llamada al parser.
// Layout: [det_idx][lm_idx] → {x, y} en píxeles absolutos del espacio de red (0-640).
// El probe del pipeline lee esto y dibuja los círculos.
struct Det10gLandmarks {
    float pts[NUM_LANDMARKS][2];   // [0..4][x,y] en píxeles de red
};

static std::mutex              g_lm_mutex;
static std::vector<Det10gLandmarks> g_landmarks;   // una entrada por detección del último frame

extern "C" {
    // Función pública para que el probe del pipeline lea los landmarks.
    // Retorna el número de detecciones y copia los datos al buffer del caller.
    // El caller debe llamar esta función desde el mismo thread del probe.
    int Det10g_GetLandmarks(Det10gLandmarks* out, int max_count)
    {
        std::lock_guard<std::mutex> lk(g_lm_mutex);
        int n = (int)g_landmarks.size();
        if (n > max_count) n = max_count;
        if (out && n > 0) memcpy(out, g_landmarks.data(), n * sizeof(Det10gLandmarks));
        return n;
    }
}

static bool g_debug = false;
static bool g_debug_init = false;

extern "C" bool NvDsInferParseCustomDet10g(
    std::vector<NvDsInferLayerInfo> const& outputLayersInfo,
    NvDsInferNetworkInfo const& networkInfo,
    NvDsInferParseDetectionParams const& detectionParams,
    std::vector<NvDsInferObjectDetectionInfo>& objectList)
{
    if (!g_debug_init) {
        g_debug = (std::getenv("DET10G_DEBUG") != nullptr);
        g_debug_init = true;
    }

    if (g_debug) {
        fprintf(stderr, "[Det10g] Parser invocado. Output layers: %zu\n",
                outputLayersInfo.size());
    }

    StrideTensor tensors[NUM_STRIDES];

    for (int si = 0; si < NUM_STRIDES; ++si) {
        int stride = STRIDES[si];
        int feat_h = (networkInfo.height + stride - 1) / stride;
        int feat_w = (networkInfo.width  + stride - 1) / stride;
        tensors[si].feat_h      = feat_h;
        tensors[si].feat_w      = feat_w;
        tensors[si].num_anchors = feat_h * feat_w * ANCHORS_PER_LOCATION;
    }

    // ONNX output order (confirmed via trtexec --dumpLayerInfo):
    // [0] 448: scores stride 8   [1, 12800, 1]
    // [1] 471: scores stride 16  [1, 3200,  1]
    // [2] 494: scores stride 32  [1, 800,   1]
    // [3] 451: bboxes stride 8   [1, 12800, 4]
    // [4] 474: bboxes stride 16  [1, 3200,  4]
    // [5] 497: bboxes stride 32  [1, 800,   4]
    // [6] 454: kps    stride 8   [1, 12800, 10]
    // [7] 477: kps    stride 16  [1, 3200,  10]
    // [8] 500: kps    stride 32  [1, 800,   10]
    if (outputLayersInfo.size() < 9) {
        fprintf(stderr, "[Det10g] ERROR: Expected 9 output tensors, got %zu\n",
                outputLayersInfo.size());
        return false;
    }

    tensors[0].scores = static_cast<const float*>(outputLayersInfo[0].buffer);
    tensors[1].scores = static_cast<const float*>(outputLayersInfo[1].buffer);
    tensors[2].scores = static_cast<const float*>(outputLayersInfo[2].buffer);

    tensors[0].bboxes = static_cast<const float*>(outputLayersInfo[3].buffer);
    tensors[1].bboxes = static_cast<const float*>(outputLayersInfo[4].buffer);
    tensors[2].bboxes = static_cast<const float*>(outputLayersInfo[5].buffer);

    tensors[0].kps = static_cast<const float*>(outputLayersInfo[6].buffer);
    tensors[1].kps = static_cast<const float*>(outputLayersInfo[7].buffer);
    tensors[2].kps = static_cast<const float*>(outputLayersInfo[8].buffer);

    float conf_threshold = 0.5f;
    if (!detectionParams.perClassPreclusterThreshold.empty())
        conf_threshold = detectionParams.perClassPreclusterThreshold[0];

    const float input_w = static_cast<float>(networkInfo.width);
    const float input_h = static_cast<float>(networkInfo.height);

    std::vector<Det10gLandmarks> new_landmarks;
    int total_detections = 0;

    for (int si = 0; si < NUM_STRIDES; ++si) {
        const StrideTensor& t = tensors[si];
        int stride    = STRIDES[si];
        int feat_h    = t.feat_h;
        int feat_w    = t.feat_w;
        int num_anchors = t.num_anchors;
        int stride_det  = 0;

        if (!t.scores || !t.bboxes || !t.kps) {
            fprintf(stderr, "[Det10g] ERROR: Null tensor for stride %d\n", stride);
            continue;
        }

        for (int y = 0; y < feat_h; ++y) {
            for (int x = 0; x < feat_w; ++x) {
                float cx = (x + 0.5f) * stride;
                float cy = (y + 0.5f) * stride;

                for (int a = 0; a < ANCHORS_PER_LOCATION; ++a) {
                    int idx = (y * feat_w + x) * ANCHORS_PER_LOCATION + a;
                    if (idx >= num_anchors) continue;

                    float score = t.scores[idx];
                    if (score < conf_threshold) continue;

                    // Decodificar bbox (FCOS: l, t, r, b desde centro)
                    int bb = idx * 4;
                    if (bb + 3 >= num_anchors * 4) continue;

                    float x1 = std::max(0.0f, std::min(cx - t.bboxes[bb + 0] * stride, input_w));
                    float y1 = std::max(0.0f, std::min(cy - t.bboxes[bb + 1] * stride, input_h));
                    float x2 = std::max(0.0f, std::min(cx + t.bboxes[bb + 2] * stride, input_w));
                    float y2 = std::max(0.0f, std::min(cy + t.bboxes[bb + 3] * stride, input_h));

                    if (x2 <= x1 || y2 <= y1) continue;

                    // Decodificar 5 landmarks (FCOS: offset desde centro × stride)
                    // Formato tensor: [idx*10 + lm*2 + 0/1] → dx/dy respecto al centro
                    Det10gLandmarks lm;
                    int kp_base = idx * 10;
                    if (kp_base + 9 < num_anchors * 10) {
                        for (int k = 0; k < NUM_LANDMARKS; ++k) {
                            float px = cx + t.kps[kp_base + k * 2 + 0] * stride;
                            float py = cy + t.kps[kp_base + k * 2 + 1] * stride;
                            lm.pts[k][0] = std::max(0.0f, std::min(px, input_w));
                            lm.pts[k][1] = std::max(0.0f, std::min(py, input_h));
                        }
                    } else {
                        memset(&lm, 0, sizeof(lm));
                    }
                    new_landmarks.push_back(lm);

                    NvDsInferObjectDetectionInfo det;
                    det.classId            = 0;
                    det.detectionConfidence = score;
                    det.left   = x1;
                    det.top    = y1;
                    det.width  = x2 - x1;
                    det.height = y2 - y1;
                    objectList.push_back(det);

                    stride_det++;
                    total_detections++;
                }
            }
        }

        if (g_debug) {
            fprintf(stderr, "[Det10g] Stride %d: %d detections\n", stride, stride_det);
        }
    }

    // Publicar landmarks para que el probe los dibuje
    {
        std::lock_guard<std::mutex> lk(g_lm_mutex);
        g_landmarks = std::move(new_landmarks);
    }

    if (g_debug) {
        fprintf(stderr, "[Det10g] Total detections: %d\n", total_detections);
    }

    return true;
}

CHECK_CUSTOM_PARSE_FUNC_PROTOTYPE(NvDsInferParseCustomDet10g);
