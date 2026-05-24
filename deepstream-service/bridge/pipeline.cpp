#include "pipeline.h"

#include <gst/gst.h>
#include <glib.h>
#include <math.h>
#include <cuda_runtime_api.h>

Pipeline::Pipeline(const char* yml_path)
    : yml_path_(yml_path),
      enc_enable_(FALSE),
      preprocess_(nullptr),
      pgie_(nullptr),
      sgie0_(nullptr),
      sgie1_(nullptr)
{
    memset(&codec_status_, 0, sizeof(codec_status_));
}

Pipeline::~Pipeline()
{
}

Pipeline* Pipeline::create_from_env(const char* yml_path)
{
    const char* model = g_getenv("MODEL");
    if (!model) model = "yolo-v9";

    if (g_strcmp0(model, "yolo-v9") == 0)
        return new PipelineYolo(yml_path);
    if (g_strcmp0(model, "peoplenet") == 0)
        return new PipelinePeoplenet(yml_path);
    if (g_strcmp0(model, "peoplenet-facedetect") == 0)
        return new PipelineFacedetect(yml_path);
    if (g_strcmp0(model, "trafficcamnet-lpd-lpr") == 0)
        return new PipelineTrafficcamnet(yml_path);

    g_printerr("Unknown MODEL=%s, falling back to yolo-v9\n", model);
    return new PipelineYolo(yml_path);
}

bool Pipeline::parse_codec()
{
    nvds_parse_codec_status((gchar*)yml_path_, "encoder", &codec_status_);
    enc_enable_ = codec_status_.enable;
    return true;
}

bool Pipeline::create_source(AppCtx& appctx)
{
    if (appctx.restServer) {
        g_print("Calling nvmultiurisrcbincreator API \n");

        appctx.nvmultiurisrcbinCreator = gst_nvmultiurisrcbincreator_init(
            0, NVDS_MULTIURISRCBIN_MODE_VIDEO, &appctx.muxConfig);
        if (!appctx.nvmultiurisrcbinCreator) {
            g_printerr("gst_nvmultiurisrcbincreator_init failed. Exiting.\n");
            return false;
        }

        GstDsNvUriSrcConfig sourceConfig;
        memset(&sourceConfig, 0, sizeof(GstDsNvUriSrcConfig));
        sourceConfig.sensorId = NULL;
        sourceConfig.uri = appctx.uri_list;
        sourceConfig.source_id = 0;
        sourceConfig.disable_passthrough = TRUE;
        if (!gst_nvmultiurisrcbincreator_add_source(
                appctx.nvmultiurisrcbinCreator, &sourceConfig)) {
            g_printerr("gst_nvmultiurisrcbincreator_add_source failed. Exiting.\n");
            return false;
        }
    } else {
        g_print("Calling gst_element_factory_make for nvmultiurisrcbin \n");
        appctx.multiuribin = gst_element_factory_make("nvmultiurisrcbin", "multiuribin");
        if (!appctx.multiuribin) {
            g_printerr("One element multiuribin could not be created. Exiting.\n");
            return false;
        }
        nvds_parse_multiurisrcbin(appctx.multiuribin, (gchar*)yml_path_, "multiurisrcbin");
    }

    return true;
}

bool Pipeline::create_encoder(AppCtx& appctx)
{
    if (!enc_enable_) return true;

    appctx.nvvidconv2 = gst_element_factory_make("nvvideoconvert", "nvvideo-converter-2");
    if (codec_status_.codec_type == 1) {
        appctx.encoder = gst_element_factory_make("nvv4l2h264enc", "nvv4l2h264encoder");
        appctx.parser = gst_element_factory_make("h264parse", "h264parse");
    } else if (codec_status_.codec_type == 2) {
        appctx.encoder = gst_element_factory_make("nvv4l2h265enc", "nvv4l2h265encoder");
        appctx.parser = gst_element_factory_make("h265parse", "h265parse");
    } else {
        g_printerr("Invalid codec type. Use codec=1 H264, codec=2 H265\n");
        return false;
    }

    appctx.queue_post_encoder = gst_element_factory_make("queue", "queue-post-encoder");

    if (!appctx.nvvidconv2 || !appctx.encoder || !appctx.parser ||
        !appctx.queue_post_encoder) {
        g_printerr("One element could not be created in encoder path. Exiting.\n");
        return false;
    }

    return true;
}

bool Pipeline::create_common(AppCtx& appctx)
{
    gboolean perf_mode = g_getenv("NVDS_SERVER_APP_PERF_MODE") &&
        !g_strcmp0(g_getenv("NVDS_SERVER_APP_PERF_MODE"), "1");

    preprocess_ = gst_element_factory_make("identity", "preprocess-plugin");
    appctx.preprocess = preprocess_;

    appctx.queue1 = gst_element_factory_make("queue", "queue1");
    appctx.queue2 = gst_element_factory_make("queue", "queue2");
    appctx.queue3 = gst_element_factory_make("queue", "queue3");
    appctx.queue4 = gst_element_factory_make("queue", "queue4");
    appctx.queue5 = gst_element_factory_make("queue", "queue5");
    appctx.queue6 = gst_element_factory_make("queue", "queue6");

    appctx.nvdslogger = gst_element_factory_make("nvdslogger", "nvdslogger");
    if (appctx.nvdslogger) {
        g_object_set(G_OBJECT(appctx.nvdslogger),
                     "fps-measurement-interval-sec", 1, NULL);
    }

    appctx.tiler = gst_element_factory_make("nvmultistreamtiler", "nvtiler");
    appctx.nvvidconv = gst_element_factory_make("nvvideoconvert", "nvvideo-converter");
    appctx.nvosd = gst_element_factory_make("nvdsosd", "nv-onscreendisplay");

    gboolean analytics_enabled = check_enable_status((gchar*)yml_path_, "analytics");
    if (!analytics_enabled) {
        appctx.nvanalytics = gst_element_factory_make("identity", "analytics");
    } else {
        appctx.nvanalytics = gst_element_factory_make("nvdsanalytics", "analytics");
        if (!appctx.nvanalytics) {
            g_printerr("nvdsanalytics element could not be created. Exiting.\n");
            return false;
        }
        nvds_parse_nvdsanalytics(appctx.nvanalytics, (gchar*)yml_path_, "analytics");
    }

    int current_device = -1;
    cudaGetDevice(&current_device);
    struct cudaDeviceProp prop;
    cudaGetDeviceProperties(&prop, current_device);

    if (perf_mode) {
        g_print("PERF_MODE Enabled\n");
        appctx.sink = gst_element_factory_make("fakesink", "nvvideo-renderer");
    } else {
        if (prop.integrated) {
            appctx.sink = gst_element_factory_make("nv3dsink", "nv3d-sink");
        } else {
            if (!enc_enable_) {
#ifdef __aarch64__
                appctx.sink = gst_element_factory_make("nv3dsink", "nvvideo-renderer");
#else
                appctx.sink = gst_element_factory_make("nveglglessink", "nvvideo-renderer");
#endif
            } else {
                appctx.sink = gst_element_factory_make("filesink", "file-sink");
                if (codec_status_.codec_type == 1) {
                    g_object_set(G_OBJECT(appctx.sink),
                                 "location", "/opt/output/out.h264", NULL);
                    g_object_set(G_OBJECT(appctx.sink), "sync", 1, NULL);
                } else if (codec_status_.codec_type == 2) {
                    g_object_set(G_OBJECT(appctx.sink), "sync", 1, NULL);
                }
            }
        }
    }

    if (!preprocess_ || !appctx.nvdslogger || !appctx.tiler ||
        !appctx.nvvidconv || !appctx.nvosd || !appctx.sink) {
        g_printerr("One common element could not be created. Exiting.\n");
        return false;
    }

    return true;
}

bool Pipeline::create_tracker(AppCtx& appctx, GstElement*& tracker, GstElement*& qt)
{
    tracker = gst_element_factory_make("nvtracker", "tracker");
    if (!tracker) {
        g_printerr("nvtracker element could not be created. Exiting.\n");
        return false;
    }
    if (nvds_parse_tracker(tracker, (gchar*)yml_path_, "tracker")
        != NVDS_YAML_PARSER_SUCCESS) {
        g_printerr("Failed to parse tracker config. Exiting.\n");
        return false;
    }
    g_print("Tracker configured successfully\n");

    qt = gst_element_factory_make("queue", "queue_t");
    if (!qt) {
        g_printerr("queue_t element could not be created. Exiting.\n");
        return false;
    }

    return true;
}

void Pipeline::configure_elements(AppCtx& appctx, guint batch_size)
{
    g_object_set(G_OBJECT(pgie_),
                 "config-file-path", "dsserver_pgie_config.yml", NULL);

    nvds_parse_gie(pgie_, (gchar*)yml_path_, "primary-gie");
    g_object_set(G_OBJECT(pgie_), "batch-size", batch_size, NULL);
    nvds_parse_osd(appctx.nvosd, (gchar*)yml_path_, "osd");

    guint tiler_rows = (guint)sqrt(batch_size);
    guint tiler_columns = (guint)ceil(1.0 * batch_size / tiler_rows);
    g_object_set(G_OBJECT(appctx.tiler), "rows", tiler_rows,
                 "columns", tiler_columns, NULL);

    nvds_parse_tiler(appctx.tiler, (gchar*)yml_path_, "tiler");

    int current_device = -1;
    cudaGetDevice(&current_device);
    struct cudaDeviceProp prop;
    cudaGetDeviceProperties(&prop, current_device);
    gboolean perf_mode = g_getenv("NVDS_SERVER_APP_PERF_MODE") &&
        !g_strcmp0(g_getenv("NVDS_SERVER_APP_PERF_MODE"), "1");

    if (!enc_enable_) {
        if (prop.integrated) {
            nvds_parse_3d_sink(appctx.sink, (gchar*)yml_path_, "sink");
        } else {
            if (perf_mode) {
                nvds_parse_fake_sink(appctx.sink, (gchar*)yml_path_, "sink");
            } else {
#ifdef __aarch64__
                nvds_parse_3d_sink(appctx.sink, (gchar*)yml_path_, "sink");
#else
                nvds_parse_egl_sink(appctx.sink, (gchar*)yml_path_, "sink");
#endif
            }
        }
    }
}

void Pipeline::add_common_to_bin(AppCtx& appctx)
{
    gboolean perf_mode = g_getenv("NVDS_SERVER_APP_PERF_MODE") &&
        !g_strcmp0(g_getenv("NVDS_SERVER_APP_PERF_MODE"), "1");

    if (perf_mode) {
        gst_bin_add_many(GST_BIN(appctx.pipeline),
                         appctx.queue1, appctx.nvdslogger,
                         appctx.queue5, appctx.sink, NULL);
    } else {
        gst_bin_add_many(GST_BIN(appctx.pipeline),
                         appctx.queue1, preprocess_,
                         appctx.queue2,
                         appctx.nvanalytics, appctx.queue6,
                         appctx.nvdslogger, appctx.tiler, appctx.queue3,
                         appctx.nvvidconv, appctx.queue4, appctx.nvosd,
                         appctx.queue5, appctx.sink, NULL);
        if (enc_enable_) {
            gst_bin_add_many(GST_BIN(appctx.pipeline),
                             appctx.nvvidconv2, appctx.encoder,
                             appctx.parser, appctx.queue_post_encoder, NULL);
        }
    }
}

bool Pipeline::link_sink(AppCtx& appctx)
{
    if (!enc_enable_) {
        if (!gst_element_link_many(appctx.queue5, appctx.sink, NULL)) {
            g_printerr("queue5->sink link failed. Exiting.\n");
            return false;
        }
    } else {
        gst_element_link_many(appctx.queue5, appctx.nvvidconv2, appctx.encoder,
                              appctx.queue_post_encoder, appctx.parser,
                              appctx.sink, NULL);
    }
    return true;
}

GstElement* Pipeline::source_bin(AppCtx& appctx) const
{
    if (appctx.restServer) {
        return gst_nvmultiurisrcbincreator_get_bin(appctx.nvmultiurisrcbinCreator);
    }
    return appctx.multiuribin;
}

GstElement* Pipeline::make(const char* factory, const char* name)
{
    GstElement* e = gst_element_factory_make(factory, name);
    if (!e) {
        g_printerr("Failed to create element %s (%s)\n", name, factory);
    }
    return e;
}

bool Pipeline::build(AppCtx& appctx)
{
    if (!parse_codec()) return false;

    appctx.pipeline = gst_pipeline_new("dsserver-pipeline");
    if (!appctx.pipeline) {
        g_printerr("dsserver-pipeline element could not be created. Exiting.\n");
        return false;
    }

    if (!create_source(appctx)) return false;

    gst_bin_add_many(GST_BIN(appctx.pipeline), source_bin(appctx), NULL);

    if (!create_encoder(appctx)) return false;
    if (!create_common(appctx)) return false;

    guint batch_size;
    if (appctx.restServer) {
        batch_size = appctx.muxConfig.maxBatchSize;
    } else {
        g_object_get(appctx.multiuribin, "max-batch-size", &batch_size, NULL);
    }

    create_inference(appctx, batch_size);
    if (!pgie_) {
        g_printerr("PGIE element not created. Exiting.\n");
        return false;
    }

    add_common_to_bin(appctx);

    configure_elements(appctx, batch_size);

    GstElement* tracker = nullptr;
    GstElement* qt = nullptr;
    if (!create_tracker(appctx, tracker, qt)) return false;

    gst_bin_add_many(GST_BIN(appctx.pipeline), tracker, qt, NULL);

    GstElement* src = source_bin(appctx);
    link_inference(appctx, src, tracker, qt);

    if (!link_sink(appctx)) return false;

    return true;
}
