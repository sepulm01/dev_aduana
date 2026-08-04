/*
 * Aduana Test — Pipeline for offline video processing with production features.
 * Redis pub/sub, TCP crop-receiver, snapshot sender, device mapping.
 * Reads MP4 files directly via file://, displays in real-time or records to MP4.
 */
#include <gst/gst.h>
#include <glib.h>
#include <stdio.h>
#include <string.h>
#include <cuda_runtime_api.h>
#include <hiredis/hiredis.h>
#include <time.h>
#include <unordered_map>
#include <utility>
#include <vector>
#include <sstream>
#include <netdb.h>
#include <unistd.h>
#include <sys/socket.h>

#include "gstnvdsmeta.h"
#include "gstnvdsinfer.h"
#include "nvds_yml_parser.h"
#include "nvds_analytics_meta.h"
#include "snapshot_sender.h"

extern "C" {
#include "nvds_obj_encode.h"
}

#define MUXER_OUTPUT_WIDTH  1920
#define MUXER_OUTPUT_HEIGHT 1080
#define TILED_OUTPUT_WIDTH  1280
#define TILED_OUTPUT_HEIGHT 720
#define MAX_SOURCES 128
#define MAX_CLASSES 16
#define DEFAULT_MIN_CONFIDENCE 0.6f
#define CONFIDENCE_CONFIG "/opt/computer_vision/config/confidence_thresholds.txt"

#define CROP_RECEIVER_HOST "crop-receiver"
#define CROP_RECEIVER_PORT 12347
#define CROP_END_MARKER "END!"
#define CROP_MIN_BBOX_PX 20
#define CROP_MAX_FPS 15
#define CROP_MIN_OCR_BYTES 3000
#define SNAP_COOLDOWN_US 500000

#define RETURN_ON_PARSER_ERROR(parse_expr) \
    if (NVDS_YAML_PARSER_SUCCESS != parse_expr) { \
        g_printerr("Error in parsing: %s\n", #parse_expr); \
        return 1; \
    }

static float g_class_confidence[MAX_CLASSES];

struct TruckKey { int sid; guint64 oid; };
bool operator==(const TruckKey& a, const TruckKey& b) { return a.sid == b.sid && a.oid == b.oid; }
struct TruckKeyHash { size_t operator()(const TruckKey& k) const { return (size_t)k.sid * 31 + (size_t)k.oid; } };
struct TruckTrack { bool crossed = false; bool in_roi = false; };
static std::unordered_map<TruckKey, TruckTrack, TruckKeyHash> g_trucks;
static FILE* g_csv = nullptr;

/* Redis */
static redisContext* pub_ctx = NULL;
static GMutex redis_mutex;
static int source_to_device[MAX_SOURCES];
static guint64 frame_counts[MAX_SOURCES];
static const gchar* g_sources_key = "deepstream:sources:aduana:1";

/* Crop TCP */
static NvDsObjEncCtxHandle g_crop_enc_ctx = NULL;
static struct { int fd = -1; bool ok = false; } g_crop_sock;
static guint64 g_crop_obj_ctr = 0;

#pragma pack(push, 1)
struct CropPacket {
    uint32_t device_id, source_id, class_id;
    uint64_t object_id;
    float confidence, bbox_left, bbox_top, bbox_width, bbox_height;
    uint32_t frame_num;
    uint64_t timestamp_ms;
    uint32_t jpeg_size;
};
#pragma pack(pop)

/* Snapshots */
static SnapshotSender* roi_snap = NULL;
static SnapshotSender* lc_snap  = NULL;
static SnapshotSender* oc_snap  = NULL;
static guint64 last_snap_time = 0;

/* --- Helpers --- */
static bool center_inside(NvDsObjectMeta* outer, NvDsObjectMeta* inner) {
    float icx = inner->detector_bbox_info.org_bbox_coords.left
              + inner->detector_bbox_info.org_bbox_coords.width  * 0.5f;
    float icy = inner->detector_bbox_info.org_bbox_coords.top
              + inner->detector_bbox_info.org_bbox_coords.height * 0.5f;
    float ol = outer->detector_bbox_info.org_bbox_coords.left;
    float ot = outer->detector_bbox_info.org_bbox_coords.top;
    float or_ = ol + outer->detector_bbox_info.org_bbox_coords.width;
    float ob  = ot + outer->detector_bbox_info.org_bbox_coords.height;
    return icx >= ol && icx <= or_ && icy >= ot && icy <= ob;
}

static void load_confidence_thresholds() {
    for (int i = 0; i < MAX_CLASSES; i++)
        g_class_confidence[i] = DEFAULT_MIN_CONFIDENCE;
    FILE* f = fopen(CONFIDENCE_CONFIG, "r");
    if (!f) return;
    char line[256];
    while (fgets(line, sizeof(line), f)) {
        int cls_id = -1;
        float conf = 0.0f;
        if (sscanf(line, "%d=%f", &cls_id, &conf) == 2) {
            if (cls_id >= 0 && cls_id < MAX_CLASSES)
                g_class_confidence[cls_id] = conf;
        }
    }
    fclose(f);
    g_print("[Confidence] thresholds loaded:");
    for (int i = 0; i < 5; i++)
        g_print(" cls%d=%.2f", i, g_class_confidence[i]);
    g_print("\n");
}

/* --- Crop TCP --- */
static bool connect_crop_receiver() {
    if (g_crop_sock.ok) return true;
    g_crop_sock.fd = socket(AF_INET, SOCK_STREAM, 0);
    if (g_crop_sock.fd < 0) return false;

    struct sockaddr_in addr = {};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(CROP_RECEIVER_PORT);

    struct hostent* h = gethostbyname(CROP_RECEIVER_HOST);
    if (!h) { close(g_crop_sock.fd); g_crop_sock.fd = -1; return false; }
    memcpy(&addr.sin_addr, h->h_addr_list[0], h->h_length);

    if (connect(g_crop_sock.fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        close(g_crop_sock.fd); g_crop_sock.fd = -1; return false;
    }
    g_crop_sock.ok = true;
    g_print("[Crop] Connected to crop-receiver:%d\n", CROP_RECEIVER_PORT);
    return true;
}

static bool send_crop(const NvDsObjectMeta* om, const NvDsFrameMeta* fm,
                      int dev_id, int sid) {
    if (!g_crop_sock.ok && !connect_crop_receiver()) return false;

    for (NvDsMetaList* lum = om->obj_user_meta_list; lum; lum = lum->next) {
        NvDsUserMeta* um = (NvDsUserMeta*)lum->data;
        if (!um || um->base_meta.meta_type != NVDS_CROP_IMAGE_META) continue;
        NvDsObjEncOutParams* enc = (NvDsObjEncOutParams*)um->user_meta_data;
        if (!enc || !enc->outBuffer || enc->outLen == 0) continue;

        float fw = (float)MUXER_OUTPUT_WIDTH, fh = (float)MUXER_OUTPUT_HEIGHT;
        CropPacket pkt;
        pkt.device_id    = (uint32_t)dev_id;
        pkt.source_id    = (uint32_t)sid;
        pkt.class_id     = (uint32_t)om->class_id;
        pkt.object_id    = om->object_id;
        pkt.confidence   = om->confidence;
        pkt.bbox_left    = om->detector_bbox_info.org_bbox_coords.left / fw;
        pkt.bbox_top     = om->detector_bbox_info.org_bbox_coords.top / fh;
        pkt.bbox_width   = om->detector_bbox_info.org_bbox_coords.width / fw;
        pkt.bbox_height  = om->detector_bbox_info.org_bbox_coords.height / fh;
        pkt.frame_num    = (uint32_t)fm->frame_num;
        pkt.timestamp_ms = (uint64_t)(time(nullptr) * 1000LL);
        pkt.jpeg_size    = (uint32_t)enc->outLen;

        if (om->class_id == 3 && enc->outLen < CROP_MIN_OCR_BYTES) return false;

        ssize_t s = send(g_crop_sock.fd, enc->outBuffer, enc->outLen, MSG_NOSIGNAL);
        if (s < 0) { close(g_crop_sock.fd); g_crop_sock.ok = false; return false; }
        s = send(g_crop_sock.fd, CROP_END_MARKER, strlen(CROP_END_MARKER), MSG_NOSIGNAL);
        if (s < 0) { close(g_crop_sock.fd); g_crop_sock.ok = false; return false; }
        s = send(g_crop_sock.fd, &pkt, sizeof(pkt), MSG_NOSIGNAL);
        if (s < 0) { close(g_crop_sock.fd); g_crop_sock.ok = false; return false; }
        return true;
    }
    return false;
}

/* --- Probe: detection + analytics + Redis + crops --- */
static GstPadProbeReturn analytics_probe(GstPad* pad, GstPadProbeInfo* info,
                                         gpointer user_data) {
    static guint64 last_reload = 0;
    guint64 now = g_get_monotonic_time();

    GstBuffer* buf = GST_BUFFER(info->data);
    NvDsBatchMeta* batch_meta = gst_buffer_get_nvds_batch_meta(buf);
    if (!batch_meta) return GST_PAD_PROBE_OK;

    /* Device mapping from Redis every 5s */
    if (now - last_reload >= 5000000 && pub_ctx) {
        redisReply* r = (redisReply*)redisCommand(pub_ctx, "HGETALL %s", g_sources_key);
        if (r && r->type == REDIS_REPLY_ARRAY) {
            memset(source_to_device, -1, sizeof(source_to_device));
            for (size_t k = 0; k + 1 < r->elements; k += 2) {
                if (strchr(r->element[k]->str, ':')) continue;
                int si = atoi(r->element[k]->str);
                int di = atoi(r->element[k + 1]->str);
                if (si >= 0 && si < MAX_SOURCES) source_to_device[si] = di;
            }
        }
        if (r) freeReplyObject(r);
        last_reload = now;
    }

    for (NvDsMetaList* lf = batch_meta->frame_meta_list; lf; lf = lf->next) {
        NvDsFrameMeta* fm = (NvDsFrameMeta*)lf->data;
        int sid = fm->source_id;
        if (sid < 0 || sid >= 2) continue;
        gdouble ts_sec = GST_CLOCK_TIME_IS_VALID(fm->buf_pts)
            ? (gdouble)fm->buf_pts / (gdouble)GST_SECOND : -1.0;
        int dev_id = (sid >= 0 && sid < MAX_SOURCES) ? source_to_device[sid] : -1;

        frame_counts[sid]++;

        /* Snapshot check */
        bool has_roi = false, has_lc = false, has_oc = false;
        /* Pass 1: collect trucks + analytics events */
        std::vector<NvDsObjectMeta*> trucks;
        for (NvDsMetaList* lo = fm->obj_meta_list; lo; lo = lo->next) {
            NvDsObjectMeta* om = (NvDsObjectMeta*)lo->data;
            if (om->class_id != 4) continue;

            bool lc = false, roi = false;
            std::string rn;
            for (NvDsMetaList* lum = om->obj_user_meta_list; lum; lum = lum->next) {
                NvDsUserMeta* um = (NvDsUserMeta*)lum->data;
                if (!um) continue;
                if (um->base_meta.meta_type != NVDS_USER_OBJ_META_NVDSANALYTICS) continue;
                NvDsAnalyticsObjInfo* ai = (NvDsAnalyticsObjInfo*)um->user_meta_data;
                if (!ai) continue;
                if (!ai->lcStatus.empty()) { lc = true; has_lc = true; }
                if (!ai->ocStatus.empty()) { has_oc = true; }
                for (const auto& r : ai->roiStatus) {
                    roi = true; has_roi = true;
                    if (rn.empty()) rn = r;
                }
            }

            if (lc || roi) {
                TruckKey key = {sid, om->object_id};
                auto& st = g_trucks[key];
                if (lc && !st.crossed) {
                    st.crossed = true;
                    if (ts_sec >= 0)
                        g_print("[TRUCK] id=%lu src=%d crossed IN->OUT  ts=%.3fs\n",
                                om->object_id, sid, ts_sec);
                    if (g_csv) fprintf(g_csv, "%.3f,CROSS,%d,%lu,,,,,\n", ts_sec, sid, om->object_id);
                }
                if (roi && st.crossed && !st.in_roi) {
                    st.in_roi = true;
                    g_print("[TRUCK] id=%lu src=%d IN ROI{%s}  ts=%.3fs\n",
                            om->object_id, sid, rn.c_str(), ts_sec);
                    if (g_csv) fprintf(g_csv, "%.3f,ROI_IN,%d,%lu,,,,,%s\n",
                                       ts_sec, sid, om->object_id, rn.c_str());
                }
            }
            trucks.push_back(om);
        }

        /* Snapshot send full frame */
        if ((roi_snap || lc_snap || oc_snap) && now - last_snap_time >= SNAP_COOLDOWN_US) {
            GstMapInfo inmap = GST_MAP_INFO_INIT;
            if (gst_buffer_map(buf, &inmap, GST_MAP_READ)) {
                NvBufSurface* surf = (NvBufSurface*)inmap.data;
                if (surf) {
                    if (has_roi && roi_snap) roi_snap->send_full_frame(surf, fm, dev_id, sid);
                    if (has_lc && lc_snap)   lc_snap->send_full_frame(surf, fm, dev_id, sid);
                    if (has_oc && oc_snap)   oc_snap->send_full_frame(surf, fm, dev_id, sid);
                }
                gst_buffer_unmap(buf, &inmap);
            }
            last_snap_time = now;
        }

        /* Pass 2: Crop sending + CARGO association */
        {
            static guint64 last_crop_sent = 0;
            guint64 crop_interval = 1000000 / CROP_MAX_FPS;
            std::vector<const NvDsObjectMeta*> crop_objs;

            for (NvDsMetaList* lo = fm->obj_meta_list; lo; lo = lo->next) {
                NvDsObjectMeta* om = (NvDsObjectMeta*)lo->data;
                int cls = om->class_id;
                if (cls == 4) continue;
                if (cls != 0 && cls != 1 && cls != 3) continue;

                float w = om->detector_bbox_info.org_bbox_coords.width;
                float h = om->detector_bbox_info.org_bbox_coords.height;
                if (w < CROP_MIN_BBOX_PX || h < CROP_MIN_BBOX_PX) continue;
                if (om->confidence < g_class_confidence[cls]) continue;

                bool inside_active = false;
                for (auto* tk : trucks) {
                    auto it = g_trucks.find({sid, tk->object_id});
                    if (it != g_trucks.end() && it->second.crossed && !it->second.in_roi
                        && center_inside(tk, om)) {
                        inside_active = true; break;
                    }
                }
                if (!inside_active) continue;

                /* CARGO CSV */
                const char* cls_name = "?";
                if (cls == 0) cls_name = "con_sello";
                else if (cls == 1) cls_name = "sin_sello";
                else if (cls == 2) cls_name = "cont_data";
                else if (cls == 3) cls_name = "container_cod";

                for (auto* tk : trucks) {
                    if (center_inside(tk, om) && g_csv)
                        fprintf(g_csv, "%.3f,CARGO,%d,%lu,%s,%.2f,,\n",
                                ts_sec, sid, tk->object_id, cls_name, om->confidence);
                }

                /* Crop encoding */
                if (true) {
                    NvDsObjEncUsrArgs objData = {};
                    objData.saveImg = FALSE;
                    objData.attachUsrMeta = TRUE;
                    objData.quality = 80;
                    objData.objNum = (int)(++g_crop_obj_ctr);

                    NvBufSurface* surf = NULL;
                    GstMapInfo inmap = GST_MAP_INFO_INIT;
                    if (gst_buffer_map(buf, &inmap, GST_MAP_READ)) {
                        surf = (NvBufSurface*)inmap.data;
                        if (surf) {
                            nvds_obj_enc_process(g_crop_enc_ctx, &objData, surf, om, fm);
                            crop_objs.push_back(om);
                        }
                        gst_buffer_unmap(buf, &inmap);
                    }
                }
            }

            if (!crop_objs.empty()) {
                nvds_obj_enc_finish(g_crop_enc_ctx);
                for (auto* om : crop_objs) {
                    if (send_crop(om, fm, dev_id, sid))
                        g_print("[Crop] dev=%d src=%d cls=%d obj=%lu\n",
                                dev_id, sid, om->class_id, om->object_id);
                }
            }
        }

        /* Pass 3: Redis JSON for objects inside active trucks */
        {
            static guint64 last_json = 0;
            if (now - last_json >= 1000000 && pub_ctx) {
                last_json = now;
                float fw = (float)MUXER_OUTPUT_WIDTH, fh = (float)MUXER_OUTPUT_HEIGHT;
                std::stringstream json;
                json << "{\"source_id\":" << sid
                     << ",\"frame_num\":" << fm->frame_num
                     << ",\"ts\":" << ts_sec
                     << ",\"objects\":[";
                bool first = true;
                for (NvDsMetaList* lo = fm->obj_meta_list; lo; lo = lo->next) {
                    NvDsObjectMeta* om = (NvDsObjectMeta*)lo->data;
                    if (om->class_id == 4) continue;
                    guint64 truck_id = 0;
                    for (auto* tk : trucks) {
                        auto it = g_trucks.find({sid, tk->object_id});
                        if (it != g_trucks.end() && it->second.crossed && !it->second.in_roi
                            && center_inside(tk, om)) {
                            truck_id = tk->object_id; break;
                        }
                    }
                    if (truck_id == 0) continue;
                    if (!first) json << ",";
                    first = false;
                    json << "{\"cls\":" << om->class_id
                         << ",\"truck\":" << truck_id
                         << ",\"conf\":" << om->confidence
                         << ",\"bbox\":[" << (om->detector_bbox_info.org_bbox_coords.left/fw)
                         << "," << (om->detector_bbox_info.org_bbox_coords.top/fh)
                         << "," << (om->detector_bbox_info.org_bbox_coords.width/fw)
                         << "," << (om->detector_bbox_info.org_bbox_coords.height/fh)
                         << "]}";
                }
                json << "]}";
                if (!first) {
                    std::string msg = json.str();
                    gchar channel[64];
                    g_snprintf(channel, sizeof(channel), "device:%d:detections", dev_id);
                    g_mutex_lock(&redis_mutex);
                    redisReply* r = (redisReply*)redisCommand(pub_ctx,
                        "PUBLISH %s %b", channel, msg.c_str(), msg.size());
                    if (r) freeReplyObject(r);
                    g_mutex_unlock(&redis_mutex);
                }
            }
        }
    }

    /* FPS health */
    static guint64 last_health = 0;
    if (now - last_health >= 1000000 && pub_ctx) {
        gdouble elapsed = (gdouble)(now - last_health) / 1000000.0;
        g_mutex_lock(&redis_mutex);
        for (int i = 0; i < 2; i++) {
            if (frame_counts[i] > 0) {
                gchar k[32];
                g_snprintf(k, sizeof(k), "%d:fps", i);
                redisReply* r = (redisReply*)redisCommand(pub_ctx,
                    "HSET %s %s %d", g_sources_key, k,
                    (int)(frame_counts[i] / elapsed + 0.5));
                if (r) freeReplyObject(r);
                frame_counts[i] = 0;
            }
        }
        g_mutex_unlock(&redis_mutex);
        last_health = now;
    }

    return GST_PAD_PROBE_OK;
}

/* --- Source setup callback for RTSP sources --- */
static void source_setup_callback(GstElement* obj, GstElement* source, gpointer user_data) {
    if (g_strrstr(GST_ELEMENT_NAME(source), "rtspsrc") ||
        g_strrstr(G_OBJECT_TYPE_NAME(source), "RTSPSrc")) {
        g_object_set(G_OBJECT(source), "latency", 0, "drop-on-latency", TRUE,
                     "protocols", 4, NULL);
        g_print("[Source setup] rtspsrc latency=0 drop-on-latency=1 protocols=TCP\n");
    }
}

/* --- Source bin: uridecodebin → nvvideoconvert → queue --- */
static GstElement* create_source_bin(guint index, gchar* uri) {
    GstElement* bin = gst_bin_new(NULL);
    GstElement* uri_decode_bin = gst_element_factory_make("uridecodebin", NULL);
    if (!bin || !uri_decode_bin) return NULL;
    g_object_set(G_OBJECT(uri_decode_bin), "uri", uri, NULL);
    g_signal_connect(G_OBJECT(uri_decode_bin), "source-setup",
                     G_CALLBACK(source_setup_callback), NULL);
    GstElement* nvconv = gst_element_factory_make("nvvideoconvert", NULL);
    GstElement* conv_queue = gst_element_factory_make("queue", NULL);
    if (!nvconv || !conv_queue) return NULL;
    gst_bin_add_many(GST_BIN(bin), uri_decode_bin, nvconv, conv_queue, NULL);
    g_signal_connect(uri_decode_bin, "pad-added",
        G_CALLBACK(+[](GstElement* e, GstPad* pad, gpointer data) {
            GstPad* sp = gst_element_get_static_pad(GST_ELEMENT(data), "sink");
            if (!gst_pad_is_linked(sp)) gst_pad_link(pad, sp);
            gst_object_unref(sp);
        }), nvconv);
    if (!gst_element_link(nvconv, conv_queue)) return NULL;
    GstPad* srcpad = gst_element_get_static_pad(conv_queue, "src");
    gst_element_add_pad(bin, gst_ghost_pad_new("src", srcpad));
    gst_object_unref(srcpad);
    return bin;
}

int main(int argc, char* argv[]) {
    const char* display_env = getenv("ENABLE_DISPLAY");
    gboolean show_display = display_env ? (atoi(display_env) != 0) : FALSE;

    if (argc < 2) {
        g_printerr("Usage: %s <config.yml>\n", argv[0]);
        return 1;
    }

    load_confidence_thresholds();
    memset(source_to_device, -1, sizeof(source_to_device));

    const char* sk = getenv("DEEPSTREAM_SOURCES_KEY");
    if (sk) g_sources_key = sk;

    int do_record = 0;
    gchar record_path[512] = "/opt/computer_vision/record/output.mp4";
    int record_bitrate = 2000000, record_width = 1280, record_height = 720;
    {
        FILE* f = fopen("/opt/computer_vision/config/video_output.txt", "r");
        if (f) {
            char line[256];
            while (fgets(line, sizeof(line), f)) {
                if (g_str_has_prefix(line, "record=")) do_record = atoi(line + 7);
                else if (g_str_has_prefix(line, "output_path=")) g_strlcpy(record_path, g_strstrip(line + 12), sizeof(record_path));
                else if (g_str_has_prefix(line, "bitrate=")) record_bitrate = atoi(line + 8);
                else if (g_str_has_prefix(line, "width=")) record_width = atoi(line + 6);
                else if (g_str_has_prefix(line, "height=")) record_height = atoi(line + 7);
            }
            fclose(f);
        }
    }

    gst_init(&argc, &argv);
    GMainLoop* loop = g_main_loop_new(NULL, FALSE);

    /* Redis */
    pub_ctx = redisConnect("redis", 6379);
    if (!pub_ctx || pub_ctx->err) {
        g_printerr("Redis connection failed\n");
        if (pub_ctx) { redisFree(pub_ctx); pub_ctx = NULL; }
    } else { g_print("Redis connected\n"); }
    g_mutex_init(&redis_mutex);

    /* Crop encoder */
    g_crop_enc_ctx = nvds_obj_enc_create_context(0);
    if (!g_crop_enc_ctx) g_printerr("Crop encoder failed\n");
    g_crop_sock.fd = -1; g_crop_sock.ok = false;
    g_crop_obj_ctr = 0;

    /* Snapshots (only if SNAPSHOT_HOST is set) */
    const char* snap_host = getenv("SNAPSHOT_HOST");
    if (snap_host) {
        roi_snap = new SnapshotSender(snap_host, 12348, "roi");
        lc_snap  = new SnapshotSender(snap_host, 12348, "lc");
        oc_snap  = new SnapshotSender(snap_host, 12348, "oc");
        roi_snap->start(); lc_snap->start(); oc_snap->start();
    }

    /* Pipeline */
    GstElement *pipeline = NULL, *streammux = NULL, *pgie = NULL,
               *nvtracker = NULL, *nvds_analytics = NULL,
               *tiler = NULL, *tiler_conv = NULL,
               *nvosd = NULL, *queue1 = NULL, *queue2 = NULL;
    GstElement *record_conv = NULL, *record_caps = NULL, *record_scale_caps = NULL,
               *record_enc = NULL, *record_parse = NULL, *record_mux = NULL, *record_sink = NULL;

    pipeline = gst_pipeline_new("aduana-test-pipeline");

    const gchar* source_uris[2] = {
        "file:///opt/computer_vision/test/cam1_seg3.mp4",
        "file:///opt/computer_vision/test/cam2_seg3.mp4"
    };
    guint num_sources = 2;
    g_print("Num sources: %d\n", num_sources);

    streammux = gst_element_factory_make("nvstreammux", "stream-muxer");
    g_object_set(G_OBJECT(streammux), "batch-size", 2,
                 "batched-push-timeout", 40000,
                 "width", MUXER_OUTPUT_WIDTH, "height", MUXER_OUTPUT_HEIGHT,
                 "live-source", 0, "attach-sys-ts", FALSE, "sync-inputs", 0, NULL);
    gst_bin_add(GST_BIN(pipeline), streammux);
    RETURN_ON_PARSER_ERROR(nvds_parse_streammux(streammux, argv[1], "streammux"));

    for (guint i = 0; i < num_sources; i++) {
        GstPad *sinkpad, *srcpad;
        gchar pad_name[16] = {};
        GstElement* source_bin = create_source_bin(i, (gchar*)source_uris[i]);
        gst_bin_add(GST_BIN(pipeline), source_bin);
        g_snprintf(pad_name, 15, "sink_%u", i);
        sinkpad = gst_element_request_pad_simple(streammux, pad_name);
        srcpad = gst_element_get_static_pad(source_bin, "src");
        if (!srcpad || gst_pad_link(srcpad, sinkpad) != GST_PAD_LINK_OK) return 1;
        gst_object_unref(srcpad); gst_object_unref(sinkpad);
    }

    pgie = gst_element_factory_make("nvinfer", "primary-inference");
    nvtracker = gst_element_factory_make("nvtracker", "nvtracker");
    nvds_analytics = gst_element_factory_make("nvdsanalytics", "analytics");
    queue1 = gst_element_factory_make("queue", "q1");
    queue2 = gst_element_factory_make("queue", "q2");

    g_object_set(G_OBJECT(pgie), "config-file-path", "../models/yolov9_aduana/pgie_config.yml", NULL);
    RETURN_ON_PARSER_ERROR(nvds_parse_gie(pgie, argv[1], "primary-gie"));
    g_object_set(G_OBJECT(nvtracker),
                 "tracker-width", 640, "tracker-height", 384,
                 "ll-lib-file", "/opt/nvidia/deepstream/deepstream-8.0/lib/libnvds_nvmultiobjecttracker.so",
                 "ll-config-file", "/opt/computer_vision/config/config_tracker_NvSORT.yml", NULL);
    RETURN_ON_PARSER_ERROR(nvds_parse_nvdsanalytics(nvds_analytics, argv[1], "analytics"));

    tiler = gst_element_factory_make("nvmultistreamtiler", "tiler");
    tiler_conv = gst_element_factory_make("nvvideoconvert", "tiler-conv");
    nvosd = gst_element_factory_make("nvdsosd", "nv-onscreendisplay");
    g_object_set(G_OBJECT(tiler), "rows", 1, "columns", 2, NULL);
    RETURN_ON_PARSER_ERROR(nvds_parse_tiler(tiler, argv[1], "tiler"));
    RETURN_ON_PARSER_ERROR(nvds_parse_osd(nvosd, argv[1], "osd"));

    GstElement* out_tee = gst_element_factory_make("tee", "out-tee");
    gst_bin_add_many(GST_BIN(pipeline), streammux, queue1, pgie, queue2,
                     nvtracker, nvds_analytics, tiler, tiler_conv, nvosd, out_tee, NULL);
    if (!gst_element_link_many(streammux, queue1, pgie, queue2, nvtracker,
                                nvds_analytics, tiler, tiler_conv, nvosd, out_tee, NULL))
        { g_printerr("Pipeline link failed\n"); return 1; }

    /* Display branch */
    {
        GstElement* dq = gst_element_factory_make("queue", "display-queue");
        gst_bin_add(GST_BIN(pipeline), dq);
        GstPad* p1 = gst_element_request_pad_simple(out_tee, "src_%u");
        GstPad* p2 = gst_element_get_static_pad(dq, "sink");
        gst_pad_link(p1, p2); gst_object_unref(p1); gst_object_unref(p2);

        GstElement* ds = NULL;
        if (show_display) ds = gst_element_factory_make("nveglglessink", "display-sink");
        if (!ds) { show_display = FALSE; ds = gst_element_factory_make("fakesink", "fake-sink");
                   g_object_set(G_OBJECT(ds), "sync", FALSE, "qos", FALSE, NULL); }
        gst_bin_add(GST_BIN(pipeline), ds);
        gst_element_link(dq, ds);
    }

    /* Recording branch */
    if (do_record) {
        gchar scs[128]; g_snprintf(scs, sizeof(scs), "video/x-raw(memory:NVMM), format=NV12, width=%d, height=%d", record_width, record_height);
        record_conv = gst_element_factory_make("nvvideoconvert", "record-conv");
        record_caps = gst_element_factory_make("capsfilter", "record-caps");
        record_scale_caps = gst_element_factory_make("capsfilter", "record-scale");
        record_enc  = gst_element_factory_make("nvv4l2h264enc", "record-enc");
        record_parse = gst_element_factory_make("h264parse", "record-parse");
        record_mux  = gst_element_factory_make("mp4mux", "record-mux");
        record_sink = gst_element_factory_make("filesink", "record-sink");
        if (!record_conv || !record_caps || !record_scale_caps || !record_enc || !record_parse || !record_mux || !record_sink)
            { do_record = 0; }
        else {
            g_object_set(G_OBJECT(record_caps), "caps", gst_caps_from_string("video/x-raw(memory:NVMM), format=NV12"), NULL);
            g_object_set(G_OBJECT(record_scale_caps), "caps", gst_caps_from_string(scs), NULL);
            g_object_set(G_OBJECT(record_enc), "bitrate", record_bitrate, "iframeinterval", 30, NULL);
            g_object_set(G_OBJECT(record_mux), "fragment-duration", 1000, NULL);
            g_object_set(G_OBJECT(record_sink), "location", record_path, "sync", FALSE, NULL);
            gst_bin_add_many(GST_BIN(pipeline), record_conv, record_caps, record_scale_caps, record_enc, record_parse, record_mux, record_sink, NULL);
            if (!gst_element_link_many(record_conv, record_caps, record_scale_caps, record_enc, record_parse, record_mux, record_sink, NULL))
                { do_record = 0; }
            else {
                GstPad* p1 = gst_element_request_pad_simple(out_tee, "src_%u");
                GstPad* p2 = gst_element_get_static_pad(record_conv, "sink");
                if (!p1 || !p2 || gst_pad_link(p1, p2) != GST_PAD_LINK_OK) do_record = 0;
                if (p1) gst_object_unref(p1); if (p2) gst_object_unref(p2);
                if (do_record) g_print("[Record] %s %dx%d bitrate=%d\n", record_path, record_width, record_height, record_bitrate);
            }
        }
    } else g_print("[Record] disabled\n");

    GstPad* probe_pad = gst_element_get_static_pad(nvds_analytics, "src");
    gst_pad_add_probe(probe_pad, GST_PAD_PROBE_TYPE_BUFFER, analytics_probe, NULL, NULL);
    gst_object_unref(probe_pad);

    GstBus* bus = gst_pipeline_get_bus(GST_PIPELINE(pipeline));
    gst_bus_add_watch(bus, [](GstBus* b, GstMessage* msg, gpointer d) -> gboolean {
        GMainLoop* l = (GMainLoop*)d;
        switch (GST_MESSAGE_TYPE(msg)) {
            case GST_MESSAGE_EOS: g_print("End of stream\n"); g_main_loop_quit(l); break;
            case GST_MESSAGE_ERROR: { GError* e = NULL; gchar* dbg = NULL;
                gst_message_parse_error(msg, &e, &dbg);
                g_printerr("ERROR: %s\n%s\n", e->message, dbg ? dbg : "");
                g_free(dbg); g_error_free(e); g_main_loop_quit(l); break;
            }
            default: break;
        }
        return TRUE;
    }, loop);
    gst_object_unref(bus);

    g_print("Pipeline playing %s display\n", show_display ? "WITH" : "WITHOUT");
    g_csv = fopen("/opt/computer_vision/record/trucks.csv", "w");
    if (g_csv) fprintf(g_csv, "ts,event,src,truck,cls,conf,ocr,roi\n");
    gst_element_set_state(pipeline, GST_STATE_PLAYING);
    g_main_loop_run(loop);

    g_print("Shutting down\n");
    if (g_csv) { fclose(g_csv); g_csv = nullptr; }
    if (pub_ctx) { redisFree(pub_ctx); pub_ctx = NULL; }
    g_mutex_clear(&redis_mutex);
    if (g_crop_enc_ctx) { nvds_obj_enc_finish(g_crop_enc_ctx); nvds_obj_enc_destroy_context(g_crop_enc_ctx); }
    if (g_crop_sock.fd >= 0) close(g_crop_sock.fd);
    if (roi_snap) { roi_snap->stop(); delete roi_snap; }
    if (lc_snap)  { lc_snap->stop();  delete lc_snap;  }
    if (oc_snap)  { oc_snap->stop();  delete oc_snap;  }
    gst_element_set_state(pipeline, GST_STATE_NULL);
    gst_object_unref(pipeline);
    g_main_loop_unref(loop);
    return 0;
}
