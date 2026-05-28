/*
 * Based on deepstream-test3 with analytics probe and Redis FPS health.
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

#include "gstnvdsmeta.h"
#include "nvds_yml_parser.h"
#include "gst-nvmessage.h"

#define MAX_DISPLAY_LEN 64
#define PGIE_CLASS_ID_VEHICLE 0
#define PGIE_CLASS_ID_PERSON 2
#define OSD_PROCESS_MODE 1
#define OSD_DISPLAY_TEXT 1
#define MUXER_OUTPUT_WIDTH 1920
#define MUXER_OUTPUT_HEIGHT 1080
#define MUXER_BATCH_TIMEOUT_USEC 40000
#define TILED_OUTPUT_WIDTH 1280
#define TILED_OUTPUT_HEIGHT 720
#define GST_CAPS_FEATURES_NVMM "memory:NVMM"
#define MAX_SOURCES 128
#define FLUSH_INTERVAL_US 1000000

#define RETURN_ON_PARSER_ERROR(parse_expr) \
    if (NVDS_YAML_PARSER_SUCCESS != parse_expr) { \
        g_printerr("Error in parsing configuration file.\n"); \
        return -1; \
    }

gchar pgie_classes_str[4][32] = {"Vehicle", "TwoWheeler", "Person", "RoadSign"};
static gboolean PERF_MODE = FALSE;
static GMutex redis_mutex;
static redisContext* pub_ctx = NULL;
static int source_to_device[MAX_SOURCES];
static guint64 frame_counts[MAX_SOURCES];
static const char* g_labels[] = {"person", "bag", "face"};

static void redis_hset(const char* k, const char* f, const char* v)
{
    g_mutex_lock(&redis_mutex);
    if (pub_ctx) {
        redisReply* r = (redisReply*)redisCommand(pub_ctx, "HSET %s %s %s", k, f, v);
        if (r) freeReplyObject(r);
    }
    g_mutex_unlock(&redis_mutex);
}

static GstPadProbeReturn tiler_src_pad_buffer_probe(GstPad* pad, GstPadProbeInfo* info,
                                                      gpointer u_data)
{
    GstBuffer* buf = (GstBuffer*)info->data;
    NvDsBatchMeta* batch_meta = gst_buffer_get_nvds_batch_meta(buf);
    guint vehicle_count = 0, person_count = 0;

    for (NvDsMetaList* lf = batch_meta->frame_meta_list; lf; lf = lf->next) {
        NvDsFrameMeta* fm = (NvDsFrameMeta*)lf->data;
        for (NvDsMetaList* lo = fm->obj_meta_list; lo; lo = lo->next) {
            NvDsObjectMeta* om = (NvDsObjectMeta*)lo->data;
            if (om->class_id == PGIE_CLASS_ID_VEHICLE) vehicle_count++;
            if (om->class_id == PGIE_CLASS_ID_PERSON) person_count++;
        }
    }
    return GST_PAD_PROBE_OK;
}

static GstPadProbeReturn analytics_pad_probe(GstPad* pad, GstPadProbeInfo* info,
                                              gpointer user_data)
{
    static guint64 last_flush = 0, last_health = 0, last_reload = 0;

    GstBuffer* buf = GST_BUFFER(info->data);
    NvDsBatchMeta* batch_meta = gst_buffer_get_nvds_batch_meta(buf);
    if (!batch_meta) return GST_PAD_PROBE_OK;

    guint64 now = g_get_monotonic_time();

    if (now - last_reload >= 5000000 && pub_ctx) {
        redisReply* r = (redisReply*)redisCommand(pub_ctx, "HGETALL deepstream:sources");
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
        if (dev_id < 0) continue;

        if (now - last_flush >= FLUSH_INTERVAL_US) {
            gchar json_buf[65536];
            int off = g_snprintf(json_buf, sizeof(json_buf),
                "{\"code\":\"DeepStreamDetection\",\"action\":\"Pulse\","
                "\"timestamp\":%lld,\"data\":{\"device_id\":%d,\"source\":%d,"
                "\"frame_num\":0,\"Object\":[",
                (long long)(time(NULL) * 1000LL), dev_id, sid);

            float fw = (float)fm->source_frame_width;
            float fh = (float)fm->source_frame_height;
            if (fw <= 0) fw = 1920;
            if (fh <= 0) fh = 1080;

            int obj_count = 0;
            for (NvDsMetaList* lo = fm->obj_meta_list; lo; lo = lo->next) {
                NvDsObjectMeta* om = (NvDsObjectMeta*)lo->data;
                float left = om->detector_bbox_info.org_bbox_coords.left;
                float top = om->detector_bbox_info.org_bbox_coords.top;
                float width = om->detector_bbox_info.org_bbox_coords.width;
                float height = om->detector_bbox_info.org_bbox_coords.height;

                int rx1 = (int)(left / fw * 1600);
                int ry1 = (int)(top / fh * 900);
                int rx2 = (int)((left + width) / fw * 1600);
                int ry2 = (int)((top + height) / fh * 900);

                const char* label = g_labels[om->class_id < 3 ? om->class_id : 0];
                const char* id_field = (om->class_id == 0 || om->class_id == 2)
                    ? "HumamID" : "VehicleID";

                off += g_snprintf(json_buf + off, sizeof(json_buf) - off,
                    "%s{\"object_id\":%lu,\"class_id\":%d,"
                    "\"class_label\":\"%s\",\"confidence\":%.4f,"
                    "\"bbox\":{\"left\":%.4f,\"top\":%.4f,"
                    "\"width\":%.4f,\"height\":%.4f},"
                    "\"%s\":%lu,"
                    "\"Rect\":[%d,%d,%d,%d]}",
                    obj_count > 0 ? "," : "",
                    om->object_id, om->class_id,
                    label, om->confidence,
                    left / fw, top / fh, width / fw, height / fh,
                    id_field, om->object_id,
                    rx1, ry1, rx2, ry2);
                obj_count++;
            }
            off += g_snprintf(json_buf + off, sizeof(json_buf) - off, "]}}");

            if (obj_count > 0) {
                g_mutex_lock(&redis_mutex);
                if (pub_ctx) {
                    gchar ch[64];
                    g_snprintf(ch, sizeof(ch), "device:%d:events", dev_id);
                    redisReply* r = (redisReply*)redisCommand(pub_ctx,
                        "PUBLISH %s %s", ch, json_buf);
                    if (r) freeReplyObject(r);
                }
                g_mutex_unlock(&redis_mutex);
                g_print("[Analytics] device=%d objects=%d\n", dev_id, obj_count);
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
                    "HSET deepstream:sources %s %d", k,
                    (int)(frame_counts[i] / elapsed + 0.5));
                if (r) freeReplyObject(r);
                frame_counts[i] = 0;
            }
        }
        last_health = now;
        g_mutex_unlock(&redis_mutex);
    }

    return GST_PAD_PROBE_OK;
}

static gboolean bus_call(GstBus* bus, GstMessage* msg, gpointer data)
{
    GMainLoop* loop = (GMainLoop*)data;
    switch (GST_MESSAGE_TYPE(msg)) {
        case GST_MESSAGE_EOS:
            g_print("End of stream\n");
            g_main_loop_quit(loop);
            break;
        case GST_MESSAGE_WARNING: {
            gchar* debug = NULL;
            GError* error = NULL;
            gst_message_parse_warning(msg, &error, &debug);
            g_printerr("WARNING from %s: %s\n", GST_OBJECT_NAME(msg->src), error->message);
            g_free(debug);
            g_error_free(error);
            break;
        }
        case GST_MESSAGE_ERROR: {
            gchar* debug = NULL;
            GError* error = NULL;
            gst_message_parse_error(msg, &error, &debug);
            g_printerr("ERROR from %s: %s\n", GST_OBJECT_NAME(msg->src), error->message);
            if (debug) g_printerr("Details: %s\n", debug);
            g_free(debug);
            g_error_free(error);
            g_main_loop_quit(loop);
            break;
        }
        default:
            break;
    }
    return TRUE;
}

static void cb_newpad(GstElement* decodebin, GstPad* decoder_src_pad, gpointer data)
{
    GstCaps* caps = gst_pad_get_current_caps(decoder_src_pad);
    if (!caps) caps = gst_pad_query_caps(decoder_src_pad, NULL);
    const GstStructure* str = gst_caps_get_structure(caps, 0);
    const gchar* name = gst_structure_get_name(str);
    GstElement* source_bin = (GstElement*)data;
    GstCapsFeatures* features = gst_caps_get_features(caps, 0);

    if (!strncmp(name, "video", 5)) {
        if (gst_caps_features_contains(features, GST_CAPS_FEATURES_NVMM)) {
            GstPad* ghost = gst_element_get_static_pad(source_bin, "src");
            gst_ghost_pad_set_target(GST_GHOST_PAD(ghost), decoder_src_pad);
            gst_object_unref(ghost);
        } else {
            g_printerr("Decodebin did not pick nvidia decoder.\n");
        }
    }
}

static void decodebin_child_added(GstChildProxy* child_proxy, GObject* object,
                                   gchar* name, gpointer user_data)
{
    g_print("Decodebin child added: %s\n", name);
    if (!strncmp(name, "decodebin", 9)) {
        g_signal_connect(G_OBJECT(object), "child-added",
                         G_CALLBACK(decodebin_child_added), user_data);
    }
}

static GstElement* create_source_bin(guint index, gchar* uri)
{
    gchar bin_name[16];
    g_snprintf(bin_name, 15, "source-bin-%02d", index);
    GstElement* bin = gst_bin_new(bin_name);
    GstElement* uri_decode_bin = gst_element_factory_make("uridecodebin", "uri-decode-bin");

    if (!bin || !uri_decode_bin) return NULL;

    g_object_set(G_OBJECT(uri_decode_bin), "uri", uri, NULL);
    g_signal_connect(G_OBJECT(uri_decode_bin), "pad-added", G_CALLBACK(cb_newpad), bin);
    g_signal_connect(G_OBJECT(uri_decode_bin), "child-added",
                     G_CALLBACK(decodebin_child_added), bin);
    gst_bin_add(GST_BIN(bin), uri_decode_bin);
    gst_element_add_pad(bin, gst_ghost_pad_new_no_target("src", GST_PAD_SRC));

    return bin;
}

int main(int argc, char* argv[])
{
    GMainLoop* loop = NULL;
    GstElement *pipeline = NULL, *streammux = NULL, *sink = NULL, *pgie = NULL,
              *queue1, *queue2, *queue3, *queue4, *queue5,
              *nvvidconv = NULL, *nvosd = NULL, *tiler = NULL, *nvdslogger = NULL,
              *nvds_analytics = NULL;
    GstBus* bus = NULL;
    guint bus_watch_id;
    guint i, num_sources = 0;
    guint tiler_rows, tiler_columns;
    guint pgie_batch_size;
    gboolean yaml_config = FALSE;
    NvDsGieType pgie_type = NVDS_GIE_PLUGIN_INFER;

    int current_device = -1;
    cudaGetDevice(&current_device);
    struct cudaDeviceProp prop;
    cudaGetDeviceProperties(&prop, current_device);

    const char* enable_display_env = getenv("ENABLE_DISPLAY");
    int show_display = enable_display_env ? atoi(enable_display_env) : 1;

    if (argc < 2) {
        g_printerr("Usage: %s <yml file>\n", argv[0]);
        return -1;
    }

    gst_init(&argc, &argv);
    loop = g_main_loop_new(NULL, FALSE);

    yaml_config = (g_str_has_suffix(argv[1], ".yml") || g_str_has_suffix(argv[1], ".yaml"));
    if (yaml_config) {
        RETURN_ON_PARSER_ERROR(nvds_parse_gie_type(&pgie_type, argv[1], "primary-gie"));
    }

    pipeline = gst_pipeline_new("analytics-pipeline");
    streammux = gst_element_factory_make("nvstreammux", "stream-muxer");
    if (!pipeline || !streammux) return -1;
    gst_bin_add(GST_BIN(pipeline), streammux);

    GList* src_list = NULL;
    if (yaml_config) {
        RETURN_ON_PARSER_ERROR(nvds_parse_source_list(&src_list, argv[1], "source-list"));
        GList* temp = src_list;
        while (temp) { num_sources++; temp = temp->next; }
        g_list_free(temp);
    } else {
        num_sources = argc - 1;
    }

    for (i = 0; i < num_sources; i++) {
        GstPad *sinkpad, *srcpad;
        gchar pad_name[16] = {};
        GstElement* source_bin = NULL;

        if (yaml_config) {
            if (!src_list || !src_list->data) continue;
            source_bin = create_source_bin(i, (gchar*)src_list->data);
        } else {
            source_bin = create_source_bin(i, argv[i + 1]);
        }
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
        if (yaml_config && src_list) src_list = src_list->next;
    }
    if (yaml_config) g_list_free(src_list);

    if (pgie_type == NVDS_GIE_PLUGIN_INFER_SERVER)
        pgie = gst_element_factory_make("nvinferserver", "primary-nvinference-engine");
    else
        pgie = gst_element_factory_make("nvinfer", "primary-nvinference-engine");

    queue1 = gst_element_factory_make("queue", "queue1");
    queue2 = gst_element_factory_make("queue", "queue2");
    nvdslogger = gst_element_factory_make("nvdslogger", "nvdslogger");
    nvds_analytics = gst_element_factory_make("nvdsanalytics", "nvdsanalytics");

    if (show_display) {
        queue3 = gst_element_factory_make("queue", "queue3");
        queue4 = gst_element_factory_make("queue", "queue4");
        queue5 = gst_element_factory_make("queue", "queue5");
        tiler = gst_element_factory_make("nvmultistreamtiler", "nvtiler");
        nvvidconv = gst_element_factory_make("nvvideoconvert", "nvvideo-converter");
        nvosd = gst_element_factory_make("nvdsosd", "nv-onscreendisplay");
        sink = gst_element_factory_make("nveglglessink", "nvvideo-renderer");
    } else {
        queue3 = NULL;
        queue4 = NULL;
        queue5 = NULL;
        sink = gst_element_factory_make("fakesink", "fake-sink");
    }

    if (!pgie || !nvdslogger || !nvds_analytics || !sink) return -1;
    if (show_display && (!tiler || !nvvidconv || !nvosd)) return -1;

    if (yaml_config) {
        RETURN_ON_PARSER_ERROR(nvds_parse_streammux(streammux, argv[1], "streammux"));
        RETURN_ON_PARSER_ERROR(nvds_parse_gie(pgie, argv[1], "primary-gie"));
        g_object_get(G_OBJECT(pgie), "batch-size", &pgie_batch_size, NULL);
        if (pgie_batch_size != num_sources && num_sources > 0)
            g_object_set(G_OBJECT(pgie), "batch-size", num_sources, NULL);
        g_object_set(G_OBJECT(nvds_analytics),
                     "enable", TRUE,
                     "config-file", "config_nvdsanalytics.txt",
                     NULL);
        g_object_set(G_OBJECT(nvds_analytics),
                     "enable", TRUE,
                     "config-file", "config_nvdsanalytics.txt",
                     NULL);
        if (show_display) {
            RETURN_ON_PARSER_ERROR(nvds_parse_osd(nvosd, argv[1], "osd"));
            if (num_sources > 0) {
                tiler_rows = (guint)sqrt(num_sources);
                tiler_columns = (guint)ceil(1.0 * num_sources / tiler_rows);
                g_object_set(G_OBJECT(tiler), "rows", tiler_rows, "columns", tiler_columns, NULL);
            }
            RETURN_ON_PARSER_ERROR(nvds_parse_tiler(tiler, argv[1], "tiler"));
            RETURN_ON_PARSER_ERROR(nvds_parse_egl_sink(sink, argv[1], "sink"));
        }
    }

    bus = gst_pipeline_get_bus(GST_PIPELINE(pipeline));
    bus_watch_id = gst_bus_add_watch(bus, bus_call, loop);
    gst_object_unref(bus);

    if (show_display) {
        gst_bin_add_many(GST_BIN(pipeline), queue1, pgie, queue2, nvdslogger,
                         nvds_analytics, tiler,
                         queue3, nvvidconv, queue4, nvosd, queue5, sink, NULL);
        if (!gst_element_link_many(streammux, queue1, pgie, queue2, nvdslogger,
                                    nvds_analytics, tiler,
                                    queue3, nvvidconv, queue4, nvosd, queue5, sink, NULL)) {
            g_printerr("Elements could not be linked.\n");
            return -1;
        }
    } else {
        gst_bin_add_many(GST_BIN(pipeline), queue1, pgie, queue2, nvdslogger,
                         nvds_analytics, sink, NULL);
        g_object_set(G_OBJECT(sink), "sync", FALSE, NULL);
        if (!gst_element_link_many(streammux, queue1, pgie, queue2, nvdslogger,
                                    nvds_analytics, sink, NULL)) {
            g_printerr("Elements could not be linked.\n");
            return -1;
        }
    }

    GstPad* probe_pad = gst_element_get_static_pad(nvdslogger, "src");
    if (probe_pad) {
        gst_pad_add_probe(probe_pad, GST_PAD_PROBE_TYPE_BUFFER,
                          analytics_pad_probe, NULL, NULL);
        g_print("[Pipeline] Analytics probe added on nvdslogger src\n");
        gst_object_unref(probe_pad);
    }

    g_mutex_init(&redis_mutex);
    memset(source_to_device, -1, sizeof(source_to_device));
    memset(frame_counts, 0, sizeof(frame_counts));

    pub_ctx = redisConnect("redis", 6379);
    if (!pub_ctx || pub_ctx->err) {
        g_printerr("[Pipeline] Redis connection failed\n");
        if (pub_ctx) { redisFree(pub_ctx); pub_ctx = NULL; }
    } else {
        g_print("[Pipeline] Redis connected\n");
    }

    g_print("Using file: %s\n", argv[1]);
    gst_element_set_state(pipeline, GST_STATE_PLAYING);
    g_print("Running...\n");
    g_main_loop_run(loop);

    g_print("Stopping playback\n");
    gst_element_set_state(pipeline, GST_STATE_NULL);
    if (pub_ctx) redisFree(pub_ctx);
    gst_object_unref(GST_OBJECT(pipeline));
    g_source_remove(bus_watch_id);
    g_main_loop_unref(loop);
    return 0;
}
