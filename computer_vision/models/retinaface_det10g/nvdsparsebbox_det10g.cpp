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
    float pts[NUM_LANDMARKS][2];
    float cx, cy;
};

static std::mutex              g_lm_mutex;
static std::vector<Det10gLandmarks> g_landmarks;

extern "C" {
    int Det10g_GetLandmarks(Det10gLandmarks* out, int max_count)
    {
        std::lock_guard<std::mutex> lk(g_lm_mutex);
        int n = (int)g_landmarks.size();
        if (n > max_count) n = max_count;
        if (out && n > 0) memcpy(out, g_landmarks.data(), n * sizeof(Det10gLandmarks));
        return n;
    }

    int Det10g_FindLandmarks(float bx, float by, float bw, float bh,
                             Det10gLandmarks* out)
    {
        std::lock_guard<std::mutex> lk(g_lm_mutex);
        float best_d = 1e9f;
        int best = -1;
        float tcx = bx + bw * 0.5f;
        float tcy = by + bh * 0.5f;
        for (int i = 0; i < (int)g_landmarks.size(); i++) {
            float dx = g_landmarks[i].cx - tcx;
            float dy = g_landmarks[i].cy - tcy;
            float d = dx * dx + dy * dy;
            if (d < best_d) { best_d = d; best = i; }
        }
        if (best >= 0 && out) {
            *out = g_landmarks[best];
            return 1;
        }
        return 0;
    }
}

static bool g_debug = false;
static bool g_debug_init = false;

// Frontal-face filter thresholds — adjustable via environment variables.
// Set in docker-compose.yml under the retinaface service environment: block.
static float g_yaw_proxy_max     = 0.40f;
static float g_asym_max          = 0.50f;
static float g_roll_max          = 30.0f;
static float g_eye_w_min         = 15.0f;
static float g_nose_mouth_dy_min = 8.0f;

static void load_env_thresholds()
{
    if (g_debug_init) return;
    g_debug_init = true;
    g_debug = (std::getenv("DET10G_DEBUG") != nullptr);
    const char* s;
    if ((s = std::getenv("DET10G_YAW_PROXY_MAX")))      g_yaw_proxy_max     = (float)atof(s);
    if ((s = std::getenv("DET10G_ASYM_MAX")))           g_asym_max          = (float)atof(s);
    if ((s = std::getenv("DET10G_ROLL_MAX")))           g_roll_max          = (float)atof(s);
    if ((s = std::getenv("DET10G_EYE_W_MIN")))          g_eye_w_min         = (float)atof(s);
    if ((s = std::getenv("DET10G_NOSE_MOUTH_DY_MIN"))) g_nose_mouth_dy_min = (float)atof(s);
    if (g_debug) {
        fprintf(stderr, "[Det10g] Thresholds: yaw_proxy=%.2f asym=%.2f roll=%.1f eye_w=%.1f nose_mouth_dy=%.1f\n",
                g_yaw_proxy_max, g_asym_max, g_roll_max, g_eye_w_min, g_nose_mouth_dy_min);
    }
}

extern "C" bool NvDsInferParseCustomDet10g(
    std::vector<NvDsInferLayerInfo> const& outputLayersInfo,
    NvDsInferNetworkInfo const& networkInfo,
    NvDsInferParseDetectionParams const& detectionParams,
    std::vector<NvDsInferObjectDetectionInfo>& objectList)
{
    load_env_thresholds();

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
                    lm.cx = (x1 + x2) * 0.5f;
                    lm.cy = (y1 + y2) * 0.5f;
                    new_landmarks.push_back(lm);

                    float re_x = lm.pts[0][0], re_y = lm.pts[0][1];
                    float le_x = lm.pts[1][0], le_y = lm.pts[1][1];
                    float no_x = lm.pts[2][0], no_y = lm.pts[2][1];

                    float eye_dx = le_x - re_x;
                    float eye_dy = le_y - re_y;
                    float eye_w  = sqrtf(eye_dx * eye_dx + eye_dy * eye_dy);

                    // eye-to-eye distance too small → landmarks collapsed, face is profile
                    if (eye_w < g_eye_w_min) continue;

                    float eye_cx = (re_x + le_x) * 0.5f;
                    float yaw_proxy = fabsf(no_x - eye_cx) / eye_w;

                    float d_r = fabsf(no_x - re_x);
                    float d_l = fabsf(no_x - le_x);
                    float asym = fabsf(d_r - d_l) / (d_r + d_l + 1e-5f);

                    float roll_deg = fabsf(atan2f(eye_dy, eye_dx) * 57.29578f);

                    float mr_y = lm.pts[3][1];
                    float ml_y = lm.pts[4][1];
                    float mouth_mid_y = (mr_y + ml_y) * 0.5f;
                    float nose_mouth_dy = fabsf(no_y - mouth_mid_y);

                    // nose and mouth at same vertical level → profile face
                    if (nose_mouth_dy < g_nose_mouth_dy_min) continue;

                    if (yaw_proxy > g_yaw_proxy_max || asym > g_asym_max || roll_deg > g_roll_max) {
                        continue;
                    }

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
