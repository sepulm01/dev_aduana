/*
 * DeepStream Security App
 *
 * NVIDIA DeepStream 8.0 C++ application with:
 * - Triton (nvinferserver) for inference (YOLOv9 / TrafficCamNet / PeopleNet)
 * - Dynamic stream management via Redis commands
 * - nvdsanalytics for ROIs, line-crossing, overcrowding, direction
 * - Redis Pub/Sub metadata publishing (compatible with existing redis_event_bridge)
 *
 * Usage: deepstream-security-app <app_config.yml>
 */

#include <gst/gst.h>
#include <glib.h>
#include <stdio.h>
#include <string.h>
#include <math.h>
#include <sys/time.h>
#include <cuda_runtime_api.h>
#include <iostream>
#include <fstream>
#include <map>
#include <string>
#include <sstream>

#include "gstnvdsmeta.h"
#include "nvds_yml_parser.h"
#include "gst-nvmultiurisrcbincreator.h"
#ifndef PLATFORM_TEGRA
#include "gst-nvmessage.h"
#endif

#include "redis_publisher.hpp"
#include "stream_manager.hpp"
#include "analytics_probe.hpp"

#define DEFAULT_MUX_WIDTH 1280
#define DEFAULT_MUX_HEIGHT 720
#define DEFAULT_MUX_BATCH_TIMEOUT_USEC 40000

struct AppConfig {
    std::string redis_url = "redis://127.0.0.1:6379";
    std::string redis_commands_channel = "deepstream:commands";
    std::string redis_events_prefix = "device";
    unsigned int heartbeat_interval = 300;
    unsigned int perf_log_interval = 5;
    std::string output_video_file;

    std::string pgie_model_name = "yolov9";
    std::string pgie_config_file;
    unsigned int batch_size = 8;
    unsigned int inference_interval = 1;

    std::string tracker_lib_file;
    std::string tracker_config_file;
    unsigned int tracker_width = 960;
    unsigned int tracker_height = 544;

    bool analytics_enabled = true;
    std::string analytics_config_file;

    unsigned int tiler_width = 1280;
    unsigned int tiler_height = 720;
    bool osd_enabled = true;

    unsigned int max_batch_size = 8;
    std::map<std::string, std::string> initial_sources;
    std::map<int, int> source_to_device_map;
    std::map<int, std::string> class_labels;
};

static gboolean
bus_call(GstBus* bus, GstMessage* msg, gpointer data) {
    GMainLoop* loop = (GMainLoop*)data;
    switch (GST_MESSAGE_TYPE(msg)) {
        case GST_MESSAGE_EOS:
            g_print("End of stream\n");
            g_main_loop_quit(loop);
            break;
        case GST_MESSAGE_ERROR: {
            gchar* debug = nullptr;
            GError* error = nullptr;
            gst_message_parse_error(msg, &error, &debug);
            g_printerr("ERROR from element %s: %s\n",
                       GST_OBJECT_NAME(msg->src), error->message);
            if (debug) g_printerr("Error details: %s\n", debug);
            g_free(debug);
            g_error_free(error);
            g_main_loop_quit(loop);
            break;
        }
        case GST_MESSAGE_WARNING: {
            gchar* debug = nullptr;
            GError* error = nullptr;
            gst_message_parse_warning(msg, &error, &debug);
            g_printerr("WARNING from element %s: %s\n",
                       GST_OBJECT_NAME(msg->src), error->message);
            g_free(debug);
            g_error_free(error);
            break;
        }
        case GST_MESSAGE_ELEMENT: {
            if (gst_nvmessage_is_stream_eos(msg)) {
                guint stream_id = 0;
                if (gst_nvmessage_parse_stream_eos(msg, &stream_id)) {
                    g_print("Got EOS from stream %u\n", stream_id);
                }
            }
            break;
        }
        default:
            break;
    }
    return TRUE;
}

static gboolean
load_labels_file(const std::string& path, std::map<int, std::string>& labels) {
    std::ifstream f(path);
    if (!f.is_open()) {
        std::cerr << "Could not open labels file: " << path << std::endl;
        return FALSE;
    }
    std::string line;
    int id = 0;
    while (std::getline(f, line)) {
        if (!line.empty()) {
            labels[id] = line;
            id++;
        }
    }
    return TRUE;
}

static std::string
find_infer_config_for_model(const std::string& model_name,
                             const std::string& config_dir) {
    if (model_name == "yolov9")
        return config_dir + "/infer_configs/pgie_yolov9.txt";
    if (model_name == "trafficcamnet")
        return config_dir + "/infer_configs/pgie_trafficcamnet.txt";
    if (model_name == "peoplenet")
        return config_dir + "/infer_configs/pgie_peoplenet.txt";
    return "";
}

struct AnalyticsReloadCtx {
    GstElement* nvanalytics;
    std::string config_file;
};

static void
reload_analytics_callback(void* ctx) {
    AnalyticsReloadCtx* reload_ctx = static_cast<AnalyticsReloadCtx*>(ctx);
    if (!reload_ctx || !reload_ctx->nvanalytics) {
        g_print("Analytics reload: invalid context\n");
        return;
    }
    g_print("Analytics reload: reloading config file: %s\n", reload_ctx->config_file.c_str());
    g_object_set(G_OBJECT(reload_ctx->nvanalytics),
                 "config-file", reload_ctx->config_file.c_str(),
                 NULL);
    g_print("Analytics config reloaded successfully\n");
}

int main(int argc, char* argv[]) {
    if (argc < 2) {
        g_printerr("Usage: %s <app_config.yml>\n", argv[0]);
        return -1;
    }

    gst_init(&argc, &argv);

    AppConfig config;
    GMainLoop* loop = g_main_loop_new(NULL, FALSE);
    GMutex bincreator_lock;
    g_mutex_init(&bincreator_lock);
    guint source_id_counter = 0;

    int current_device = -1;
    cudaGetDevice(&current_device);
    struct cudaDeviceProp prop;
    cudaGetDeviceProperties(&prop, current_device);
    g_print("Using GPU %d: %s\n", current_device, prop.name);

    std::string config_file_path = argv[1];
    std::string config_dir;
    size_t last_slash = config_file_path.rfind('/');
    if (last_slash != std::string::npos) {
        config_dir = config_file_path.substr(0, last_slash);
    } else {
        config_dir = ".";
    }

    std::string labels_path = config_dir + "/labels.txt";
    load_labels_file(labels_path, config.class_labels);
    g_print("Loaded %zu class labels\n", config.class_labels.size());

    config.pgie_model_name = "yolov9";
    config.pgie_config_file = find_infer_config_for_model(config.pgie_model_name, config_dir);
    if (config.pgie_config_file.empty()) {
        g_printerr("No inference config found for model: %s\n", config.pgie_model_name.c_str());
        return -1;
    }

    config.tracker_lib_file =
        "/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so";
    config.tracker_config_file = config_dir + "/config_tracker_NvDCF_accuracy.yml";
    config.analytics_config_file = config_dir + "/config_nvdsanalytics.txt";

    config.redis_url = "redis://127.0.0.1:6379";
    config.redis_commands_channel = "deepstream:commands";
    config.max_batch_size = 3;

    config.source_to_device_map[0] = 5;

    g_print("Configuration loaded:\n");
    g_print("  PGIE model: %s\n", config.pgie_model_name.c_str());
    g_print("  PGIE config: %s\n", config.pgie_config_file.c_str());
    g_print("  Tracker config: %s\n", config.tracker_config_file.c_str());
    g_print("  Analytics config: %s\n", config.analytics_config_file.c_str());
    g_print("  Redis: %s\n", config.redis_url.c_str());
    g_print("  Redis commands channel: %s\n", config.redis_commands_channel.c_str());

    RedisPublisher redis_pub(config.redis_url);
    if (!redis_pub.connect()) {
        g_printerr("Failed to connect to Redis. Continuing without Redis.\n");
    }

    GstElement* pipeline = gst_pipeline_new("deepstream-security-pipeline");
    if (!pipeline) {
        g_printerr("Failed to create pipeline. Exiting.\n");
        return -1;
    }

    GstDsNvStreammuxConfig mux_config;
    memset(&mux_config, 0, sizeof(GstDsNvStreammuxConfig));
    mux_config.pipeline_width = 1280;
    mux_config.pipeline_height = 720;
    mux_config.batched_push_timeout = 40000;
    mux_config.live_source = TRUE;
    mux_config.batch_size = 3;
    mux_config.maxBatchSize = 3;

    void* multiuri_bin_creator = gst_nvmultiurisrcbincreator_init(
        0, NVDS_MULTIURISRCBIN_MODE_VIDEO, &mux_config);
    if (!multiuri_bin_creator) {
        g_printerr("Failed to create nvmultiurisrcbincreator. Exiting.\n");
        return -1;
    }

    (void)multiuri_bin_creator;

    GstElement* source_bin = gst_nvmultiurisrcbincreator_get_bin(multiuri_bin_creator);
    gst_bin_add(GST_BIN(pipeline), source_bin);

    GstElement* queue1 = gst_element_factory_make("queue", "queue1");
    GstElement* queue2 = gst_element_factory_make("queue", "queue2");
    GstElement* queue3 = gst_element_factory_make("queue", "queue3");
    GstElement* queue4 = gst_element_factory_make("queue", "queue4");
    GstElement* queue5 = gst_element_factory_make("queue", "queue5");

    GstElement* pgie = gst_element_factory_make("nvinfer", "primary-nvinference-engine");
    if (!pgie) {
        g_printerr("Failed to create nvinfer element. Exiting.\n");
        return -1;
    }
    g_object_set(G_OBJECT(pgie), "config-file-path", config.pgie_config_file.c_str(), NULL);
    g_object_set(G_OBJECT(pgie), "batch-size", (guint)config.max_batch_size, NULL);
    g_object_set(G_OBJECT(pgie), "interval", config.inference_interval, NULL);

    GstElement* nvtracker = gst_element_factory_make("nvtracker", "nvtracker");
    if (!nvtracker) {
        g_printerr("Failed to create nvtracker element. Exiting.\n");
        return -1;
    }
    g_object_set(G_OBJECT(nvtracker),
                 "ll-lib-file", config.tracker_lib_file.c_str(), NULL);
    g_object_set(G_OBJECT(nvtracker),
                 "ll-config-file", config.tracker_config_file.c_str(), NULL);
    g_object_set(G_OBJECT(nvtracker),
                 "tracker-width", config.tracker_width, NULL);
    g_object_set(G_OBJECT(nvtracker),
                 "tracker-height", config.tracker_height, NULL);

    GstElement* nvanalytics = gst_element_factory_make("nvdsanalytics", "nvdsanalytics");
    if (!nvanalytics) {
        g_printerr("Failed to create nvdsanalytics element. Exiting.\n");
        return -1;
    }
    g_object_set(G_OBJECT(nvanalytics),
                 "config-file", config.analytics_config_file.c_str(), NULL);

    GstElement* nvdslogger = gst_element_factory_make("nvdslogger", "nvdslogger");
    if (nvdslogger) {
        g_object_set(G_OBJECT(nvdslogger),
                     "fps-measurement-interval-sec", config.perf_log_interval, NULL);
    }

    guint tiler_rows = (guint)sqrt(config.max_batch_size);
    guint tiler_columns = (guint)ceil(1.0 * config.max_batch_size / tiler_rows);
    GstElement* tiler = gst_element_factory_make("nvmultistreamtiler", "nvtiler");
    if (!tiler) {
        g_printerr("Failed to create nvmultistreamtiler element. Exiting.\n");
        return -1;
    }
    g_object_set(G_OBJECT(tiler),
                 "rows", tiler_rows,
                 "columns", tiler_columns,
                 "width", config.tiler_width,
                 "height", config.tiler_height,
                 NULL);

    GstElement* nvvidconv = gst_element_factory_make("nvvideoconvert", "nvvideo-converter");
    if (!nvvidconv) {
        g_printerr("Failed to create nvvideoconvert element. Exiting.\n");
        return -1;
    }

    GstElement* nvosd = gst_element_factory_make("nvdsosd", "nv-onscreendisplay");
    if (!nvosd) {
        g_printerr("Failed to create nvosd element. Exiting.\n");
        return -1;
    }
    g_object_set(G_OBJECT(nvosd),
                 "process-mode", 0,
                 NULL);

    GstElement* fake_sink = gst_element_factory_make("fakesink", "nvvideo-renderer");
    g_object_set(G_OBJECT(fake_sink), "sync", FALSE, "async", FALSE, NULL);

    if (!queue1 || !queue2 || !queue3 || !queue4 || !queue5 ||
        !pgie || !nvtracker || !nvanalytics || !nvdslogger ||
        !tiler || !nvvidconv || !nvosd || !fake_sink) {
        g_printerr("One or more elements could not be created. Exiting.\n");
        return -1;
    }

    gst_bin_add_many(GST_BIN(pipeline),
                     queue1, pgie, queue2, nvtracker, queue3,
                     nvanalytics, queue4, nvdslogger, tiler,
                     queue5, nvvidconv, nvosd, fake_sink, NULL);

    if (!gst_element_link_many(source_bin, queue1, pgie, queue2,
                               nvtracker, queue3, nvanalytics, queue4,
                               nvdslogger, tiler, queue5, nvvidconv,
                               nvosd, fake_sink, NULL)) {
        g_printerr("Failed to link pipeline elements. Exiting.\n");
        return -1;
    }

    AnalyticsProbe analytics_probe(&redis_pub);
    analytics_probe.set_source_to_device_map(config.source_to_device_map);
    analytics_probe.set_labels(config.class_labels);

    GstPad* analytics_src_pad = gst_element_get_static_pad(nvanalytics, "src");
    if (analytics_src_pad) {
        gst_pad_add_probe(analytics_src_pad, GST_PAD_PROBE_TYPE_BUFFER,
                          analytics_src_pad_buffer_probe,
                          &analytics_probe, NULL);
        gst_object_unref(analytics_src_pad);
    }

    StreamManager stream_mgr(multiuri_bin_creator, &bincreator_lock,
                             &source_id_counter, config.redis_url,
                             config.redis_commands_channel,
                             config.max_batch_size);
    AnalyticsReloadCtx analytics_reload_ctx = {nvanalytics, config.analytics_config_file};
    stream_mgr.set_reload_analytics_cb(reload_analytics_callback, &analytics_reload_ctx);
    stream_mgr.start();

    GstBus* bus = gst_pipeline_get_bus(GST_PIPELINE(pipeline));
    guint bus_watch_id = gst_bus_add_watch(bus, bus_call, loop);
    gst_object_unref(bus);

    g_print("Setting pipeline to PLAYING state...\n");
    gst_element_set_state(pipeline, GST_STATE_PLAYING);

    g_print("DeepStream Security App running. Press Ctrl+C to stop.\n");
    g_main_loop_run(loop);

    g_print("Stopping pipeline...\n");
    gst_element_set_state(pipeline, GST_STATE_NULL);

    stream_mgr.stop();

    gst_nvmultiurisrcbincreator_deinit(multiuri_bin_creator);
    g_mutex_clear(&bincreator_lock);

    gst_object_unref(GST_OBJECT(pipeline));
    g_source_remove(bus_watch_id);
    g_main_loop_unref(loop);

    g_print("Cleanup complete. Exiting.\n");
    return 0;
}