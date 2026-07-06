/*
 * Pipeline for Aduana container inspection - 4-class YOLOv9 + crop extraction.
 * Based on deepstream-test3 with analytics probe and Redis health.
 */

#include <gst/gst.h>
#include <glib.h>
#include <stdio.h>
#include <math.h>
#include <string.h>
#include <sys/time.h>
#include <cuda_runtime_api.h>
#include <hiredis/hiredis.h>
#include <time.h>
#include <string>
#include <sstream>

#include "gstnvdsmeta.h"
#include "gstnvdsinfer.h"
#include "nvds_yml_parser.h"
#include "gst-nvmessage.h"
#include "nvds_analytics_meta.h"
#include "snapshot_sender.h"

#include <vector>
#include <netdb.h>
#include <unistd.h>
#include <sys/socket.h>

#define CROP_RECEIVER_HOST "crop-receiver"
#define CROP_RECEIVER_PORT 12347
#define CROP_END_MARKER "END!"
#define CROP_MIN_BBOX_PX 20
#define CROP_MAX_FPS 15

#pragma pack(push, 1)
struct CropPacket {
    uint32_t device_id;
    uint32_t source_id;
    uint32_t class_id;
    uint64_t object_id;
    float    confidence;
    float    bbox_left;
    float    bbox_top;
    float    bbox_width;
    float    bbox_height;
    uint64_t timestamp_ms;
    uint32_t jpeg_size;
};
#pragma pack(pop)

static gboolean yaml_has_section(const gchar* file, const gchar* section) {
    gchar key[256];
    g_snprintf(key, sizeof(key), "%s:", section);
    FILE* fp = fopen(file, "r");
    if (!fp) return FALSE;
    gchar line[512];
    gboolean found = FALSE;
    while (fgets(line, sizeof(line), fp)) {
        if (strncmp(line, key, strlen(key)) == 0) { found = TRUE; break; }
    }
    fclose(fp);
    return found;
}

static int yaml_read_int(const gchar* file, const gchar* key, int default_val) {
    gchar search[256];
    g_snprintf(search, sizeof(search), "%s:", key);
    FILE* fp = fopen(file, "r");
    if (!fp) return default_val;
    gchar line[512];
    int result = default_val;
    while (fgets(line, sizeof(line), fp)) {
        if (strncmp(line, search, strlen(search)) == 0) {
            result = atoi(line + strlen(search));
            break;
        }
    }
    fclose(fp);
    return result;
}

#define MAX_DISPLAY_LEN 64
#define MUXER_OUTPUT_WIDTH 1920
#define MUXER_OUTPUT_HEIGHT 1080
#define MUXER_BATCH_TIMEOUT_USEC 40000
#define TILED_OUTPUT_WIDTH 1280
#define TILED_OUTPUT_HEIGHT 720
#define GST_CAPS_FEATURES_NVMM "memory:NVMM"
#define MAX_SOURCES 128
#define FLUSH_INTERVAL_US 200000
#define SNAP_COOLDOWN_US 3000000

#define RETURN_ON_PARSER_ERROR(parse_expr) \
    if (NVDS_YAML_PARSER_SUCCESS != parse_expr) { \
        g_printerr("Error in parsing configuration file.\n"); \
        return -1; \
    }

static GMutex redis_mutex;
static redisContext* pub_ctx = NULL;
static int source_to_device[MAX_SOURCES];
static guint64 frame_counts[MAX_SOURCES];
static const char* g_sources_key = "deepstream:sources:main";

static NvDsObjEncCtxHandle g_crop_enc_ctx = NULL;
static guint64 g_crop_obj_ctr = 0;

struct CropPending {
    NvDsObjectMeta* om;
    NvDsFrameMeta*  fm;
    int             dev_id;
    int             source_id;
};

struct ProbeData {
    SnapshotSender* roi_snap;
    SnapshotSender* lc_snap;
    SnapshotSender* oc_snap;
    guint64 last_snap_time;
};

struct CropSocket {
    int  fd;
    bool ok;
};
static CropSocket g_crop_sock = { -1, false };

static bool connect_crop_receiver() {
    if (g_crop_sock.ok) return true;
    if (g_crop_sock.fd >= 0) { close(g_crop_sock.fd); g_crop_sock.fd = -1; }

    struct addrinfo hints = {}, *res = NULL;
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    char port_str[8];
    g_snprintf(port_str, sizeof(port_str), "%d", CROP_RECEIVER_PORT);
    if (getaddrinfo(CROP_RECEIVER_HOST, port_str, &hints, &res) != 0 || !res) {
        g_printerr("[Crop] DNS lookup failed for %s\n", CROP_RECEIVER_HOST);
        return false;
    }
    int fd = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (fd < 0) { freeaddrinfo(res); return false; }
    if (connect(fd, res->ai_addr, res->ai_addrlen) < 0) {
        close(fd);
        freeaddrinfo(res);
        g_printerr("[Crop] connect to %s:%d failed\n", CROP_RECEIVER_HOST, CROP_RECEIVER_PORT);
        return false;
    }
    freeaddrinfo(res);
    g_crop_sock.fd = fd;
    g_crop_sock.ok = true;
    g_print("[Crop] Connected to %s:%d\n", CROP_RECEIVER_HOST, CROP_RECEIVER_PORT);
    return true;
}

static void close_crop_socket() {
    if (g_crop_sock.fd >= 0) { close(g_crop_sock.fd); g_crop_sock.fd = -1; }
    g_crop_sock.ok = false;
}

static bool send_crop(const CropPending& cp) {
    if (!g_crop_sock.ok && !connect_crop_receiver()) return false;

    for (NvDsMetaList* lum = cp.om->obj_user_meta_list; lum; lum = lum->next) {
        NvDsUserMeta* um = (NvDsUserMeta*)lum->data;
        if (!um || um->base_meta.meta_type != NVDS_CROP_IMAGE_META) continue;
        NvDsObjEncOutParams* enc = (NvDsObjEncOutParams*)um->user_meta_data;
        if (!enc || !enc->outBuffer || enc->outLen == 0) continue;

        float fw = (float)MUXER_OUTPUT_WIDTH;
        float fh = (float)MUXER_OUTPUT_HEIGHT;

        CropPacket pkt;
        pkt.device_id    = (uint32_t)cp.dev_id;
        pkt.source_id    = (uint32_t)cp.source_id;
        pkt.class_id     = (uint32_t)cp.om->class_id;
        pkt.object_id    = cp.om->object_id;
        pkt.confidence   = cp.om->confidence;
        pkt.bbox_left    = cp.om->detector_bbox_info.org_bbox_coords.left   / fw;
        pkt.bbox_top     = cp.om->detector_bbox_info.org_bbox_coords.top    / fh;
        pkt.bbox_width   = cp.om->detector_bbox_info.org_bbox_coords.width  / fw;
        pkt.bbox_height  = cp.om->detector_bbox_info.org_bbox_coords.height / fh;
        pkt.timestamp_ms = (uint64_t)(time(nullptr) * 1000LL);
        pkt.jpeg_size    = (uint32_t)enc->outLen;

        auto safe_send = [&](const void* data, size_t len) -> bool {
            ssize_t s = send(g_crop_sock.fd, data, len, MSG_NOSIGNAL);
            if (s < 0) { close_crop_socket(); return false; }
            return true;
        };

        if (!safe_send(enc->outBuffer, enc->outLen)) return false;
        if (!safe_send(CROP_END_MARKER, strlen(CROP_END_MARKER))) return false;
        if (!safe_send(&pkt, sizeof(pkt))) return false;

        return true;
    }
    return false;
}

static void publish_detection_json(int dev_id, int source_id,
                                    NvDsFrameMeta* fm, guint64 now_us) {
    if (!pub_ctx) return;
    gchar channel[64];
    g_snprintf(channel, sizeof(channel), "device:%d:detections", dev_id);

    std::stringstream json;
    json << "{\"device_id\":" << dev_id
         << ",\"source_id\":" << source_id
         << ",\"frame_num\":" << fm->frame_num
          << ",\"timestamp_ms\":" << ((guint64)time(nullptr) * 1000LL)
         << ",\"objects\":[";

    bool first = true;
    for (NvDsMetaList* lo = fm->obj_meta_list; lo; lo = lo->next) {
        NvDsObjectMeta* om = (NvDsObjectMeta*)lo->data;
        if (!first) json << ",";
        first = false;
        float fw = (float)MUXER_OUTPUT_WIDTH;
        float fh = (float)MUXER_OUTPUT_HEIGHT;
        json << "{\"class_id\":" << om->class_id
             << ",\"object_id\":" << om->object_id
             << ",\"confidence\":" << om->confidence
             << ",\"bbox\":{"
             << "\"left\":" << (om->detector_bbox_info.org_bbox_coords.left / fw)
             << ",\"top\":" << (om->detector_bbox_info.org_bbox_coords.top / fh)
             << ",\"width\":" << (om->detector_bbox_info.org_bbox_coords.width / fw)
             << ",\"height\":" << (om->detector_bbox_info.org_bbox_coords.height / fh)
             << "}}";
    }
    json << "]}";

    std::string msg = json.str();
    g_mutex_lock(&redis_mutex);
    redisReply* r = (redisReply*)redisCommand(pub_ctx,
        "PUBLISH %s %b", channel, msg.c_str(), msg.size());
    if (r) freeReplyObject(r);
    g_mutex_unlock(&redis_mutex);
}

static GstPadProbeReturn analytics_pad_probe(GstPad* pad, GstPadProbeInfo* info,
                                              gpointer user_data) {
    static guint64 last_flush = 0, last_health = 0, last_reload = 0;

    ProbeData* pd = (ProbeData*)user_data;
    GstBuffer* buf = GST_BUFFER(info->data);
    NvDsBatchMeta* batch_meta = gst_buffer_get_nvds_batch_meta(buf);
    if (!batch_meta) return GST_PAD_PROBE_OK;

    guint64 now = g_get_monotonic_time();

    if (now - last_reload >= 5000000 && pub_ctx) {
        redisReply* r = (redisReply*)redisCommand(pub_ctx, "HGETALL %s", g_sources_key);
        if (r && r->type == REDIS_REPLY_ARRAY) {
            g_mutex_lock(&redis_mutex);
            memset(source_to_device, -1, sizeof(source_to_device));
            for (size_t k = 0; k + 1 < r->elements; k += 2) {
                const char* key = r->element[k]->str;
                if (strchr(key, ':')) continue;
                int sid = atoi(key);
                int dev = atoi(r->element[k + 1]->str);
                if (sid >= 0 && sid < MAX_SOURCES) source_to_device[sid] = dev;
            }
            g_mutex_unlock(&redis_mutex);
        }
        if (r) freeReplyObject(r);
        last_reload = now;
    }

    for (NvDsMetaList* lf = batch_meta->frame_meta_list; lf; lf = lf->next) {
        NvDsFrameMeta* fm = (NvDsFrameMeta*)lf->data;
        int sid = fm->source_id;
        if (sid >= 0 && sid < MAX_SOURCES) frame_counts[sid]++;

        int dev_id = (sid >= 0 && sid < MAX_SOURCES) ? source_to_device[sid] : -1;

        publish_detection_json(dev_id, sid, fm, now);

        if ((pd->roi_snap || pd->lc_snap || pd->oc_snap) &&
            now - pd->last_snap_time >= SNAP_COOLDOWN_US) {
            bool has_obj_in_roi = false, has_obj_in_lc = false, has_obj_in_oc = false;
            for (NvDsMetaList* lo = fm->obj_meta_list; lo; lo = lo->next) {
                NvDsObjectMeta* om = (NvDsObjectMeta*)lo->data;
                for (NvDsMetaList* lum = om->obj_user_meta_list; lum; lum = lum->next) {
                    NvDsUserMeta* um = (NvDsUserMeta*)lum->data;
                    if (!um) continue;
                    if (um->base_meta.meta_type == NVDS_USER_OBJ_META_NVDSANALYTICS) {
                        NvDsAnalyticsObjInfo* ai = (NvDsAnalyticsObjInfo*)um->user_meta_data;
                        if (ai && ai->roiStatus.size() > 0) has_obj_in_roi = true;
                        if (ai && ai->lcStatus.size() > 0) has_obj_in_lc = true;
                        if (ai && ai->ocStatus.size() > 0) has_obj_in_oc = true;
                    }
                }
            }
            if (has_obj_in_roi || has_obj_in_lc || has_obj_in_oc) {
                GstMapInfo inmap = GST_MAP_INFO_INIT;
                if (gst_buffer_map(buf, &inmap, GST_MAP_READ)) {
                    NvBufSurface* surf = (NvBufSurface*)inmap.data;
                    if (surf) {
                        if (has_obj_in_roi && pd->roi_snap)
                            pd->roi_snap->send_full_frame(surf, fm, dev_id, sid);
                        if (has_obj_in_lc && pd->lc_snap)
                            pd->lc_snap->send_full_frame(surf, fm, dev_id, sid);
                        if (has_obj_in_oc && pd->oc_snap)
                            pd->oc_snap->send_full_frame(surf, fm, dev_id, sid);
                    }
                    gst_buffer_unmap(buf, &inmap);
                }
                pd->last_snap_time = now;
            }
        }

        if (g_crop_enc_ctx) {
            static guint64 last_crop_sent = 0;
            guint64 crop_interval = 1000000 / CROP_MAX_FPS;
            std::vector<CropPending> crop_queue;

            for (NvDsMetaList* lo = fm->obj_meta_list; lo; lo = lo->next) {
                NvDsObjectMeta* om = (NvDsObjectMeta*)lo->data;

                float w = om->detector_bbox_info.org_bbox_coords.width;
                float h = om->detector_bbox_info.org_bbox_coords.height;
                if (w < CROP_MIN_BBOX_PX || h < CROP_MIN_BBOX_PX) continue;
                if (now - last_crop_sent < crop_interval) continue;

                CropPending cp;
                cp.om        = om;
                cp.fm        = fm;
                cp.dev_id    = dev_id;
                cp.source_id = sid;

                NvDsObjEncUsrArgs objData = {};
                objData.saveImg       = FALSE;
                objData.attachUsrMeta = TRUE;
                objData.quality       = 80;
                objData.objNum        = (int)(++g_crop_obj_ctr);

                GstMapInfo inmap = GST_MAP_INFO_INIT;
                if (!gst_buffer_map(buf, &inmap, GST_MAP_READ)) continue;
                NvBufSurface* surf = (NvBufSurface*)inmap.data;
                if (surf) {
                    nvds_obj_enc_process(g_crop_enc_ctx, &objData, surf, om, fm);
                    crop_queue.push_back(cp);
                }
                gst_buffer_unmap(buf, &inmap);
                last_crop_sent = now;
            }

            if (!crop_queue.empty()) {
                nvds_obj_enc_finish(g_crop_enc_ctx);
                for (auto& cp : crop_queue) {
                    if (send_crop(cp)) {
                        g_print("[Crop] dev=%d src=%d cls=%d obj=%lu\n",
                                cp.dev_id, cp.source_id,
                                cp.om->class_id, cp.om->object_id);
                    }
                }
            }
        }
    }

    if (now - last_flush >= FLUSH_INTERVAL_US) last_flush = now;

    if (now - last_health >= 1000000 && pub_ctx) {
        g_mutex_lock(&redis_mutex);
        gdouble elapsed = (gdouble)(now - last_health) / 1000000.0;
        for (int i = 0; i < MAX_SOURCES; i++) {
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

static gboolean bus_call(GstBus* bus, GstMessage* msg, gpointer data) {
    GMainLoop* loop = (GMainLoop*)data;
    switch (GST_MESSAGE_TYPE(msg)) {
        case GST_MESSAGE_EOS:
            g_print("End of stream\n");
            g_main_loop_quit(loop);
            break;
        case GST_MESSAGE_ERROR: {
            gchar* debug;
            GError* error;
            gst_message_parse_error(msg, &error, &debug);
            g_printerr("ERROR from element %s: %s\n", GST_OBJECT_NAME(msg->src), error->message);
            g_printerr("Debug info: %s\n", (debug) ? debug : "none");
            g_error_free(error);
            g_free(debug);
            g_main_loop_quit(loop);
            break;
        }
        default:
            break;
    }
    return TRUE;
}

static void source_pad_added(GstElement* el, GstPad* pad, gpointer data) {
    GstPad* sink = (GstPad*)data;
    GstCaps* caps = gst_pad_get_current_caps(pad);
    const GstStructure* str = gst_caps_get_structure(caps, 0);
    const gchar* name = gst_structure_get_name(str);
    if (!strncmp(name, "video", 5) || !strncmp(name, "video/x-raw", 11)) {
        gst_pad_link(pad, sink);
    }
    gst_caps_unref(caps);
}

static GstElement* create_source_bin(guint index, gchar* uri) {
    GstElement* bin = gst_bin_new(NULL);
    GstElement* uri_decode_bin = gst_element_factory_make("uridecodebin", NULL);
    if (!bin || !uri_decode_bin) {
        g_printerr("Failed to create source bin\n");
        return NULL;
    }

    g_object_set(G_OBJECT(uri_decode_bin), "uri", uri, NULL);

    GstElement* nvconv = gst_element_factory_make("nvvideoconvert", NULL);
    GstElement* conv_queue = gst_element_factory_make("queue", NULL);
    if (!nvconv || !conv_queue) {
        g_printerr("Failed to create nvvideoconvert or queue\n");
        return NULL;
    }

    gst_bin_add_many(GST_BIN(bin), uri_decode_bin, nvconv, conv_queue, NULL);

    GstPad* nvconv_sink_pad = gst_element_get_static_pad(nvconv, "sink");
    g_signal_connect(G_OBJECT(uri_decode_bin), "pad-added",
                     G_CALLBACK(source_pad_added), nvconv_sink_pad);
    gst_object_unref(nvconv_sink_pad);

    gst_element_link_many(nvconv, conv_queue, NULL);

    GstPad* pad = gst_element_get_static_pad(conv_queue, "src");
    GstPad* ghost = gst_ghost_pad_new("src", pad);
    gst_pad_set_active(ghost, TRUE);
    gst_element_add_pad(bin, ghost);
    gst_object_unref(pad);

    return bin;
}

int main(int argc, char* argv[]) {
    GMainLoop* loop = NULL;
    GstElement *pipeline = NULL, *streammux = NULL, *sink = NULL, *pgie = NULL,
               *nvtracker = NULL,
               *queue1 = NULL, *queue2 = NULL, *queue3 = NULL, *queue4 = NULL, *queue5 = NULL,
               *nvvidconv = NULL, *nvosd = NULL, *tiler = NULL;
    GstBus* bus = NULL;
    guint bus_watch_id;
    guint i, num_sources = 0;
    guint pgie_batch_size;
    gboolean yaml_config = FALSE;

    int current_device = -1;
    cudaGetDevice(&current_device);
    struct cudaDeviceProp prop;
    cudaGetDeviceProperties(&prop, current_device);

    const char* enable_display_env = getenv("ENABLE_DISPLAY");
    int show_display = enable_display_env ? atoi(enable_display_env) : 0;

    const char* sources_key_env = getenv("DEEPSTREAM_SOURCES_KEY");
    if (sources_key_env) g_sources_key = sources_key_env;

    if (argc < 2) {
        g_printerr("Usage: %s <config.yml>\n", argv[0]);
        return -1;
    }

    gst_init(&argc, &argv);
    loop = g_main_loop_new(NULL, FALSE);

    yaml_config = (g_str_has_suffix(argv[1], ".yml") || g_str_has_suffix(argv[1], ".yaml"));
    if (!yaml_config) {
        g_printerr("A YAML configuration file is required.\n");
        return -1;
    }

    pub_ctx = redisConnect("redis", 6379);
    if (!pub_ctx || pub_ctx->err) {
        g_printerr("Redis connection failed: %s\n",
                   pub_ctx ? pub_ctx->errstr : "unknown");
        if (pub_ctx) redisFree(pub_ctx);
        pub_ctx = NULL;
    } else {
        g_print("Redis connected\n");
    }

    g_mutex_init(&redis_mutex);

    pipeline = gst_pipeline_new("analytics-pipeline");
    streammux = gst_element_factory_make("nvstreammux", "stream-muxer");
    if (!pipeline || !streammux) return -1;
    gst_bin_add(GST_BIN(pipeline), streammux);

    GList* src_list = NULL;
    RETURN_ON_PARSER_ERROR(nvds_parse_source_list(&src_list, argv[1], "source-list"));
    GList* temp = src_list;
    while (temp) { num_sources++; temp = temp->next; }
    g_print("Num sources: %d\n", num_sources);

    for (i = 0; i < num_sources; i++) {
        GstPad *sinkpad, *srcpad;
        gchar pad_name[16] = {};
        GstElement* source_bin = NULL;

        if (!src_list || !src_list->data) continue;
        source_bin = create_source_bin(i, (gchar*)src_list->data);
        if (!source_bin) return -1;

        gst_bin_add(GST_BIN(pipeline), source_bin);
        g_snprintf(pad_name, 15, "sink_%u", i);
        sinkpad = gst_element_request_pad_simple(streammux, pad_name);
        if (!sinkpad) return -1;
        srcpad = gst_element_get_static_pad(source_bin, "src");
        if (!srcpad || gst_pad_link(srcpad, sinkpad) != GST_PAD_LINK_OK) {
            if (srcpad) gst_object_unref(srcpad);
            gst_object_unref(sinkpad);
            return -1;
        }
        gst_object_unref(srcpad);
        gst_object_unref(sinkpad);
        src_list = src_list->next;
    }
    g_list_free(src_list);

    pgie = gst_element_factory_make("nvinfer", "primary-inference");
    nvtracker = gst_element_factory_make("nvtracker", "nvtracker");
    queue1 = gst_element_factory_make("queue", "queue1");
    queue2 = gst_element_factory_make("queue", "queue2");

    if (show_display) {
        queue3 = gst_element_factory_make("queue", "queue3");
        queue4 = gst_element_factory_make("queue", "queue4");
        queue5 = gst_element_factory_make("queue", "queue5");
        tiler = gst_element_factory_make("nvmultistreamtiler", "nvtiler");
        nvvidconv = gst_element_factory_make("nvvideoconvert", "nvvideo-converter");
        nvosd = gst_element_factory_make("nvdsosd", "nv-onscreendisplay");
        sink = gst_element_factory_make("nveglglessink", "nvvideo-renderer");
    } else {
        sink = gst_element_factory_make("fakesink", "fake-sink");
    }

    if (!pgie || !nvtracker || !sink) {
        g_printerr("Failed to create one or more elements\n");
        return -1;
    }
    if (show_display && (!tiler || !nvvidconv || !nvosd)) {
        g_printerr("Failed to create display elements\n");
        return -1;
    }

    RETURN_ON_PARSER_ERROR(nvds_parse_streammux(streammux, argv[1], "streammux"));
    RETURN_ON_PARSER_ERROR(nvds_parse_gie(pgie, argv[1], "primary-gie"));
    g_object_get(G_OBJECT(pgie), "batch-size", &pgie_batch_size, NULL);
    if (num_sources > 0) {
        guint target = MIN(pgie_batch_size, num_sources);
        g_object_set(G_OBJECT(pgie), "batch-size", target, NULL);
    }
    g_object_set(G_OBJECT(nvtracker),
                 "tracker-width", 640,
                 "tracker-height", 384,
                 "ll-lib-file",
                 "/opt/nvidia/deepstream/deepstream-8.0/lib/libnvds_nvmultiobjecttracker.so",
                 "ll-config-file", "config_tracker_IOU.yml",
                 NULL);
    if (show_display) {
        RETURN_ON_PARSER_ERROR(nvds_parse_osd(nvosd, argv[1], "osd"));
        if (num_sources > 0) {
            guint tiler_rows = (guint)sqrt(num_sources);
            guint tiler_columns = (guint)ceil(1.0 * num_sources / tiler_rows);
            g_object_set(G_OBJECT(tiler), "rows", tiler_rows, "columns", tiler_columns, NULL);
        }
        RETURN_ON_PARSER_ERROR(nvds_parse_tiler(tiler, argv[1], "tiler"));
        RETURN_ON_PARSER_ERROR(nvds_parse_egl_sink(sink, argv[1], "sink"));
    }

    memset(source_to_device, -1, sizeof(source_to_device));
    memset(frame_counts, 0, sizeof(frame_counts));

    ProbeData probeData;
    probeData.roi_snap = NULL;
    probeData.lc_snap = NULL;
    probeData.oc_snap = NULL;
    probeData.last_snap_time = 0;

    g_crop_enc_ctx = nvds_obj_enc_create_context(0);
    if (!g_crop_enc_ctx) {
        g_printerr("Failed to create crop encoder context\n");
    } else {
        connect_crop_receiver();
    }

    bus = gst_pipeline_get_bus(GST_PIPELINE(pipeline));
    bus_watch_id = gst_bus_add_watch(bus, bus_call, loop);
    gst_object_unref(bus);

    if (show_display) {
        gst_bin_add_many(GST_BIN(pipeline), queue1, pgie, queue2, nvtracker,
                         tiler,
                         queue3, nvvidconv, queue4, nvosd, queue5, sink, NULL);

        if (!gst_element_link_many(streammux, queue1, pgie, queue2, nvtracker, NULL)) {
            g_printerr("streammux to nvtracker link failed\n");
            return -1;
        }
        if (!gst_element_link_many(nvtracker, tiler,
                                    queue3, nvvidconv, queue4, nvosd, queue5, sink, NULL)) {
            g_printerr("tracker to display link failed\n");
            return -1;
        }
    } else {
        gst_bin_add_many(GST_BIN(pipeline), queue1, pgie, queue2, nvtracker,
                         sink, NULL);

        if (!gst_element_link_many(streammux, queue1, pgie, queue2, nvtracker,
                                    sink, NULL)) {
            g_printerr("streammux to sink link failed\n");
            return -1;
        }
    }

    GstPad* probe_pad = gst_element_get_static_pad(nvtracker, "src");
    if (!probe_pad) {
        g_printerr("Unable to get tracker src pad\n");
        return -1;
    }
    gst_pad_add_probe(probe_pad, GST_PAD_PROBE_TYPE_BUFFER,
                      analytics_pad_probe, &probeData, NULL);
    gst_object_unref(probe_pad);

    g_print("Pipeline ready. Setting to PLAYING\n");
    gst_element_set_state(pipeline, GST_STATE_PLAYING);

    g_main_loop_run(loop);

    g_print("Shutting down...\n");
    g_source_remove(bus_watch_id);
    gst_element_set_state(pipeline, GST_STATE_NULL);

    close_crop_socket();
    if (g_crop_enc_ctx) {
        nvds_obj_enc_destroy_context(g_crop_enc_ctx);
        g_crop_enc_ctx = NULL;
    }

    if (pub_ctx) {
        redisFree(pub_ctx);
        pub_ctx = NULL;
    }
    g_mutex_clear(&redis_mutex);

    gst_object_unref(pipeline);
    g_main_loop_unref(loop);

    return 0;
}
