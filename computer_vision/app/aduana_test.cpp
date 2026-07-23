/*
 * Aduana Test — Standalone pipeline for offline video processing.
 * No external dependencies: no crop-receiver, no Redis, no MediaMTX.
 * Reads MP4 files directly via file://, displays in real-time or records to MP4.
 */
#include <gst/gst.h>
#include <glib.h>
#include <stdio.h>
#include <string.h>
#include <cairo.h>
#include <cuda_runtime_api.h>

#include "gstnvdsmeta.h"
#include "gstnvdsinfer.h"
#include "nvds_yml_parser.h"
#include "nvds_analytics_meta.h"

#define MUXER_OUTPUT_WIDTH  1920
#define MUXER_OUTPUT_HEIGHT 1080
#define TILED_OUTPUT_WIDTH  1280
#define TILED_OUTPUT_HEIGHT 720
#define MAX_SOURCES 128
#define MAX_CLASSES 16
#define DEFAULT_MIN_CONFIDENCE 0.6f
#define CONFIDENCE_CONFIG "/opt/computer_vision/config/confidence_thresholds.txt"

#define RETURN_ON_PARSER_ERROR(parse_expr) \
    if (NVDS_YAML_PARSER_SUCCESS != parse_expr) { \
        g_printerr("Error in parsing: %s\n", #parse_expr); \
        return 1; \
    }

static float g_class_confidence[MAX_CLASSES];

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

/* --- Cairo overlay for IN/OUT zones on the tiled frame --- */
static void zone_overlay_draw(GstElement* overlay, cairo_t* cr,
                              guint width, guint height, gpointer user_data) {
    int tile_w = width / 2;
    for (int t = 0; t < 2; t++) {
        int x0 = t * tile_w;
        int cx = x0 + tile_w / 2;
        float in_g, in_r, out_g, out_r;
        if (t == 0) {
            in_g = 0.15f; in_r = 0.0f;
            out_g = 0.0f; out_r = 0.15f;
        } else {
            in_g = 0.0f; in_r = 0.15f;
            out_g = 0.15f; out_r = 0.0f;
        }
        cairo_set_source_rgba(cr, in_r, in_g, 0.0, 0.25);
        cairo_rectangle(cr, x0, 0, tile_w / 2, height);
        cairo_fill(cr);
        cairo_set_source_rgba(cr, out_r, out_g, 0.0, 0.25);
        cairo_rectangle(cr, cx, 0, tile_w / 2, height);
        cairo_fill(cr);
        cairo_set_source_rgba(cr, 1.0, 1.0, 1.0, 0.9);
        cairo_set_line_width(cr, 3.0);
        cairo_move_to(cr, cx, 0);
        cairo_line_to(cr, cx, height);
        cairo_stroke(cr);
        cairo_select_font_face(cr, "Sans", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_BOLD);
        cairo_set_font_size(cr, 22.0);
        cairo_set_source_rgba(cr, 1.0, 1.0, 1.0, 1.0);
        cairo_move_to(cr, x0 + tile_w * 0.20, 34);
        cairo_show_text(cr, t == 0 ? "IN" : "OUT");
        cairo_move_to(cr, cx + tile_w * 0.20, 34);
        cairo_show_text(cr, t == 0 ? "OUT" : "IN");
    }
}

/* --- Pad probe for per-object IN/OUT overlay on class 4 (truck) --- */
static GstPadProbeReturn osd_overlay_probe(GstPad* pad, GstPadProbeInfo* info,
                                           gpointer user_data) {
    GstBuffer* buf = GST_BUFFER(info->data);
    NvDsBatchMeta* batch_meta = gst_buffer_get_nvds_batch_meta(buf);
    if (!batch_meta) return GST_PAD_PROBE_OK;

    for (NvDsMetaList* lf = batch_meta->frame_meta_list; lf; lf = lf->next) {
        NvDsFrameMeta* fm = (NvDsFrameMeta*)lf->data;
        int sid = fm->source_id;

        for (NvDsMetaList* lo = fm->obj_meta_list; lo; lo = lo->next) {
            NvDsObjectMeta* om = (NvDsObjectMeta*)lo->data;
            if (om->class_id != 4) continue;

            float cx = om->detector_bbox_info.org_bbox_coords.left +
                       om->detector_bbox_info.org_bbox_coords.width / 2.0f;
            float norm_x = cx / (float)MUXER_OUTPUT_WIDTH;

            const char* zone;
            if (sid == 0) {
                zone = (norm_x < 0.5) ? "IN" : "OUT";
            } else {
                zone = (norm_x < 0.5) ? "OUT" : "IN";
            }

            om->text_params.display_text = g_strdup(zone);
            om->text_params.x_offset = -10;
            om->text_params.y_offset = -10;
            om->text_params.font_params.font_size = 16;
            om->text_params.font_params.font_color.red   = (zone[0] == 'I') ? 0.0f : 1.0f;
            om->text_params.font_params.font_color.green = (zone[0] == 'I') ? 1.0f : 0.0f;
            om->text_params.font_params.font_color.blue  = 0.0f;
            om->text_params.font_params.font_color.alpha = 1.0f;
            om->text_params.set_bg_clr = 1;
            om->text_params.text_bg_clr.red   = (zone[0] == 'I') ? 0.0f : 0.5f;
            om->text_params.text_bg_clr.green = (zone[0] == 'I') ? 0.4f : 0.0f;
            om->text_params.text_bg_clr.blue  = 0.0f;
            om->text_params.text_bg_clr.alpha = 0.7f;
        }
    }
    return GST_PAD_PROBE_OK;
}

/* --- Source setup callback for RTSP sources --- */
static void source_setup_callback(GstElement* obj, GstElement* source, gpointer user_data) {
    if (g_strrstr(GST_ELEMENT_NAME(source), "rtspsrc") ||
        g_strrstr(G_OBJECT_TYPE_NAME(source), "RTSPSrc")) {
        g_object_set(G_OBJECT(source),
            "latency", 0,
            "drop-on-latency", TRUE,
            "protocols", 4, /* TCP */
            NULL);
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
            GstElement* conv = GST_ELEMENT(data);
            GstPad* sinkpad = gst_element_get_static_pad(conv, "sink");
            if (!gst_pad_is_linked(sinkpad))
                gst_pad_link(pad, sinkpad);
            gst_object_unref(sinkpad);
        }), nvconv);

    if (!gst_element_link(nvconv, conv_queue)) {
        g_printerr("nvconv → queue link failed\n");
        return NULL;
    }

    /* Add ghost pad */
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

    /* --- read recording config --- */
    int do_record = 0;
    gchar record_path[512] = "/opt/computer_vision/record/output.mp4";
    int record_bitrate = 2000000;
    int record_width = 1280, record_height = 720;
    {
        FILE* f = fopen("/opt/computer_vision/config/video_output.txt", "r");
        if (f) {
            char line[256];
            while (fgets(line, sizeof(line), f)) {
                if (g_str_has_prefix(line, "record="))
                    do_record = atoi(line + 7);
                else if (g_str_has_prefix(line, "output_path="))
                    g_strlcpy(record_path, g_strstrip(line + 12), sizeof(record_path));
                else if (g_str_has_prefix(line, "bitrate="))
                    record_bitrate = atoi(line + 8);
                else if (g_str_has_prefix(line, "width="))
                    record_width = atoi(line + 6);
                else if (g_str_has_prefix(line, "height="))
                    record_height = atoi(line + 7);
            }
            fclose(f);
        }
    }

    gst_init(&argc, &argv);
    GMainLoop* loop = g_main_loop_new(NULL, FALSE);

    /* --- elements --- */
    GstElement *pipeline = NULL, *streammux = NULL, *pgie = NULL,
               *nvtracker = NULL, *nvds_analytics = NULL,
               *tiler = NULL, *tiler_conv = NULL,
               *zone_preconv = NULL, *zone_postconv = NULL,
               *zone_queue = NULL, *zone_overlay = NULL,
               *nvosd = NULL,
               *queue1 = NULL, *queue2 = NULL;
    GstElement *record_conv = NULL, *record_caps = NULL, *record_scale_caps = NULL,
               *record_enc = NULL, *record_parse = NULL, *record_mux = NULL,
               *record_sink = NULL;

    pipeline = gst_pipeline_new("aduana-test-pipeline");

    /* --- sources: hardcoded file URIs for testing --- */
    const gchar* source_uris[2] = {
        "file:///opt/computer_vision/test/cam1_60s.mp4",
        "file:///opt/computer_vision/test/cam2_60s.mp4"
    };
    guint num_sources = 2;
    g_print("Num sources: %d\n", num_sources);

    streammux = gst_element_factory_make("nvstreammux", "stream-muxer");
    if (!streammux) { g_printerr("streammux failed\n"); return 1; }

    g_object_set(G_OBJECT(streammux), "batch-size", 2,
                 "batched-push-timeout", 40000,
                 "width", MUXER_OUTPUT_WIDTH, "height", MUXER_OUTPUT_HEIGHT,
                 "live-source", 0,
                 "attach-sys-ts", FALSE,
                 "sync-inputs", 0, NULL);
    gst_bin_add(GST_BIN(pipeline), streammux);
    RETURN_ON_PARSER_ERROR(nvds_parse_streammux(streammux, argv[1], "streammux"));

    for (guint i = 0; i < num_sources; i++) {
        GstPad *sinkpad, *srcpad;
        gchar pad_name[16] = {};
        GstElement* source_bin = create_source_bin(i, (gchar*)source_uris[i]);
        if (!source_bin) return 1;

        gst_bin_add(GST_BIN(pipeline), source_bin);
        g_snprintf(pad_name, 15, "sink_%u", i);
        sinkpad = gst_element_request_pad_simple(streammux, pad_name);
        if (!sinkpad) return 1;
        srcpad = gst_element_get_static_pad(source_bin, "src");
        if (!srcpad || gst_pad_link(srcpad, sinkpad) != GST_PAD_LINK_OK) {
            if (srcpad) gst_object_unref(srcpad);
            gst_object_unref(sinkpad);
            return 1;
        }
        gst_object_unref(srcpad);
        gst_object_unref(sinkpad);
    }

    /* --- core pipeline elements --- */
    pgie = gst_element_factory_make("nvinfer", "primary-inference");
    nvtracker = gst_element_factory_make("nvtracker", "nvtracker");
    nvds_analytics = gst_element_factory_make("nvdsanalytics", "analytics");
    queue1 = gst_element_factory_make("queue", "q1");
    queue2 = gst_element_factory_make("queue", "q2");

    if (!pgie || !nvtracker || !nvds_analytics || !queue1 || !queue2) {
        g_printerr("Failed to create core elements\n");
        return 1;
    }

    g_object_set(G_OBJECT(pgie),
                 "config-file-path", "../models/yolov9_aduana/pgie_config.yml",
                 NULL);
    RETURN_ON_PARSER_ERROR(nvds_parse_gie(pgie, argv[1], "primary-gie"));

    g_object_set(G_OBJECT(nvtracker),
                 "tracker-width", 960, "tracker-height", 544,
                 "ll-lib-file", "/opt/nvidia/deepstream/deepstream-8.0/lib/libnvds_nvmultiobjecttracker.so",
                 "ll-config-file", "/opt/nvidia/deepstream/deepstream-8.0/samples/configs/deepstream-app/config_tracker_IOU.yml",
                 NULL);

    RETURN_ON_PARSER_ERROR(nvds_parse_nvdsanalytics(nvds_analytics, argv[1], "analytics"));

    /* --- tiler + zone overlay + tee + OSD --- */
    tiler = gst_element_factory_make("nvmultistreamtiler", "tiler");
    tiler_conv = gst_element_factory_make("nvvideoconvert", "tiler-conv");
    nvosd = gst_element_factory_make("nvdsosd", "nv-onscreendisplay");
    if (!tiler || !tiler_conv || !nvosd) { g_printerr("tiler/nvosd failed\n"); return 1; }
    g_object_set(G_OBJECT(tiler), "rows", 1, "columns", 2, NULL);
    RETURN_ON_PARSER_ERROR(nvds_parse_tiler(tiler, argv[1], "tiler"));
    RETURN_ON_PARSER_ERROR(nvds_parse_osd(nvosd, argv[1], "osd"));

    zone_preconv  = gst_element_factory_make("nvvideoconvert", "zone-preconv");
    zone_postconv = gst_element_factory_make("nvvideoconvert", "zone-postconv");
    zone_queue    = gst_element_factory_make("queue", "zone-queue");
    zone_overlay  = gst_element_factory_make("cairooverlay", "zone-overlay");
    if (!zone_preconv || !zone_postconv || !zone_queue || !zone_overlay) {
        g_printerr("zone overlay elements failed\n"); return 1;
    }
    g_signal_connect(zone_overlay, "draw", G_CALLBACK(zone_overlay_draw), NULL);

    GstElement* out_tee = gst_element_factory_make("tee", "out-tee");
    if (!out_tee) { g_printerr("tee failed\n"); return 1; }

    /* --- link pipeline up to out_tee --- */
    gst_bin_add_many(GST_BIN(pipeline), streammux, queue1, pgie, queue2,
                     nvtracker, nvds_analytics, tiler, tiler_conv,
                     zone_preconv, zone_queue, zone_overlay, zone_postconv,
                     nvosd, out_tee, NULL);

    if (!gst_element_link_many(streammux, queue1, pgie, queue2, nvtracker,
                                nvds_analytics, tiler, tiler_conv,
                                zone_preconv, zone_queue, zone_overlay,
                                zone_postconv, nvosd, out_tee, NULL)) {
        g_printerr("Pipeline link failed\n");
        return 1;
    }

    /* --- display branch: tee → queue → sink --- */
    {
        GstElement* display_queue = gst_element_factory_make("queue", "display-queue");
        if (!display_queue) { g_printerr("display queue failed\n"); return 1; }
        gst_bin_add(GST_BIN(pipeline), display_queue);

        GstPad* tee_src = gst_element_request_pad_simple(out_tee, "src_%u");
        GstPad* q_sink = gst_element_get_static_pad(display_queue, "sink");
        if (!tee_src || !q_sink || gst_pad_link(tee_src, q_sink) != GST_PAD_LINK_OK) {
            g_printerr("tee → display link failed\n"); return 1;
        }
        gst_object_unref(tee_src);
        gst_object_unref(q_sink);

        GstElement* display_sink = NULL;
        if (show_display) {
            display_sink = gst_element_factory_make("nveglglessink", "display-sink");
            if (!display_sink) show_display = FALSE;
        }
        if (!show_display) {
            display_sink = gst_element_factory_make("fakesink", "fake-sink");
            g_object_set(G_OBJECT(display_sink), "sync", FALSE, "qos", FALSE, NULL);
        }
        gst_bin_add(GST_BIN(pipeline), display_sink);
        if (!gst_element_link(display_queue, display_sink)) {
            g_printerr("display link failed\n"); return 1;
        }
    }

    /* --- recording branch: tee → record --- */
    if (do_record) {
        gchar scale_caps_str[128];
        g_snprintf(scale_caps_str, sizeof(scale_caps_str),
                   "video/x-raw(memory:NVMM), format=NV12, width=%d, height=%d",
                   record_width, record_height);

        record_conv = gst_element_factory_make("nvvideoconvert", "record-conv");
        record_caps = gst_element_factory_make("capsfilter", "record-caps");
        record_scale_caps = gst_element_factory_make("capsfilter", "record-scale");
        record_enc  = gst_element_factory_make("nvv4l2h264enc", "record-enc");
        record_parse = gst_element_factory_make("h264parse", "record-parse");
        record_mux  = gst_element_factory_make("mp4mux", "record-mux");
        record_sink = gst_element_factory_make("filesink", "record-sink");

        if (!record_conv || !record_caps || !record_scale_caps ||
            !record_enc || !record_parse || !record_mux || !record_sink) {
            g_printerr("Recording elements failed, disabling\n");
            do_record = 0;
        } else {
            g_object_set(G_OBJECT(record_caps), "caps",
                gst_caps_from_string("video/x-raw(memory:NVMM), format=NV12"), NULL);
            g_object_set(G_OBJECT(record_scale_caps), "caps",
                gst_caps_from_string(scale_caps_str), NULL);
            g_object_set(G_OBJECT(record_enc),
                         "bitrate", record_bitrate, "iframeinterval", 30, NULL);
            g_object_set(G_OBJECT(record_mux), "fragment-duration", 1000, NULL);
            g_object_set(G_OBJECT(record_sink),
                         "location", record_path, "sync", FALSE, NULL);

            gst_bin_add_many(GST_BIN(pipeline), record_conv, record_caps,
                             record_scale_caps, record_enc, record_parse,
                             record_mux, record_sink, NULL);

            if (!gst_element_link_many(record_conv, record_caps,
                                       record_scale_caps, record_enc,
                                       record_parse, record_mux, record_sink, NULL)) {
                g_printerr("Recording link failed\n");
                do_record = 0;
            } else {
                GstPad* tee_pad = gst_element_request_pad_simple(out_tee, "src_%u");
                GstPad* rec_pad = gst_element_get_static_pad(record_conv, "sink");
                if (!tee_pad || !rec_pad ||
                    gst_pad_link(tee_pad, rec_pad) != GST_PAD_LINK_OK) {
                    g_printerr("tee → record link failed\n");
                    do_record = 0;
                }
                if (tee_pad) gst_object_unref(tee_pad);
                if (rec_pad) gst_object_unref(rec_pad);

                if (do_record)
                    g_print("[Record] %s %dx%d bitrate=%d\n",
                            record_path, record_width, record_height, record_bitrate);
            }
        }
    } else {
        g_print("[Record] disabled\n");
    }

    /* --- pad probe for IN/OUT overlay --- */
    GstPad* osd_pad = gst_element_get_static_pad(nvosd, "sink");
    gst_pad_add_probe(osd_pad, GST_PAD_PROBE_TYPE_BUFFER,
                      osd_overlay_probe, NULL, NULL);
    gst_object_unref(osd_pad);

    /* --- bus watch --- */
    GstBus* bus = gst_pipeline_get_bus(GST_PIPELINE(pipeline));
    gst_bus_add_watch(bus, [](GstBus* b, GstMessage* msg, gpointer d) -> gboolean {
        GMainLoop* l = (GMainLoop*)d;
        switch (GST_MESSAGE_TYPE(msg)) {
            case GST_MESSAGE_EOS:
                g_print("End of stream\n");
                g_main_loop_quit(l);
                break;
            case GST_MESSAGE_ERROR: {
                GError* err = NULL;
                gchar* dbg = NULL;
                gst_message_parse_error(msg, &err, &dbg);
                g_printerr("ERROR: %s\n%s\n", err->message, dbg ? dbg : "");
                g_free(dbg); g_error_free(err);
                g_main_loop_quit(l);
                break;
            }
            default: break;
        }
        return TRUE;
    }, loop);
    gst_object_unref(bus);

    g_print("Pipeline playing %s display\n", show_display ? "WITH" : "WITHOUT");
    gst_element_set_state(pipeline, GST_STATE_PLAYING);
    g_main_loop_run(loop);

    g_print("Shutting down\n");
    gst_element_set_state(pipeline, GST_STATE_NULL);
    gst_object_unref(pipeline);
    g_main_loop_unref(loop);
    return 0;
}
