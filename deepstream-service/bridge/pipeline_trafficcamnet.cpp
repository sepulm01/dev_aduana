#include "pipeline.h"

void PipelineTrafficcamnet::create_inference(AppCtx& appctx, guint batch_size)
{
    pgie_ = make("nvinfer", "primary-nvinference-engine");
    appctx.pgie = pgie_;

    sgie0_ = make("nvinfer", "secondary-nvinference-engine0");
    if (sgie0_ && nvds_parse_gie(sgie0_, (gchar*)yml_path_, "secondary-gie0")
        == NVDS_YAML_PARSER_SUCCESS) {
        g_object_set(G_OBJECT(sgie0_), "batch-size", batch_size, NULL);
        g_print("SGIE0 configured successfully\n");
    } else if (sgie0_) {
        gst_object_unref(sgie0_);
        sgie0_ = nullptr;
        g_print("SGIE0 section incomplete or missing config-file-path, skipping\n");
    }

    sgie1_ = make("nvinfer", "secondary-nvinference-engine1");
    if (sgie1_ && nvds_parse_gie(sgie1_, (gchar*)yml_path_, "secondary-gie1")
        == NVDS_YAML_PARSER_SUCCESS) {
        g_object_set(G_OBJECT(sgie1_), "batch-size", batch_size, NULL);
        g_print("SGIE1 configured successfully\n");
    } else if (sgie1_) {
        gst_object_unref(sgie1_);
        sgie1_ = nullptr;
        g_print("SGIE1 section incomplete or missing config-file-path, skipping\n");
    }

    gst_bin_add_many(GST_BIN(appctx.pipeline), pgie_, sgie0_, sgie1_, NULL);
}

void PipelineTrafficcamnet::link_inference(AppCtx& appctx, GstElement* src,
                                            GstElement* tracker, GstElement* qt)
{
    gboolean perf_mode = g_getenv("NVDS_SERVER_APP_PERF_MODE") &&
        !g_strcmp0(g_getenv("NVDS_SERVER_APP_PERF_MODE"), "1");

    if (perf_mode) {
        if (!gst_element_link_many(src, appctx.queue1,
                                   appctx.nvdslogger, appctx.queue5, NULL)) {
            g_printerr("Elements could not be linked (perf). Exiting.\n");
        }
        return;
    }

    if (sgie1_) {
        if (!gst_element_link_many(
                src, appctx.queue1, preprocess_, pgie_, sgie0_, sgie1_,
                appctx.queue2, tracker, qt, appctx.nvanalytics,
                appctx.queue6, appctx.nvdslogger, appctx.tiler, appctx.queue3,
                appctx.nvvidconv, appctx.queue4, appctx.nvosd,
                appctx.queue5, NULL)) {
            g_printerr("PipelineTrafficcamnet link failed. Exiting.\n");
        }
    } else if (sgie0_) {
        if (!gst_element_link_many(
                src, appctx.queue1, preprocess_, pgie_, sgie0_,
                appctx.queue2, tracker, qt, appctx.nvanalytics,
                appctx.queue6, appctx.nvdslogger, appctx.tiler, appctx.queue3,
                appctx.nvvidconv, appctx.queue4, appctx.nvosd,
                appctx.queue5, NULL)) {
            g_printerr("PipelineTrafficcamnet link failed (no SGIE1). Exiting.\n");
        }
    } else {
        if (!gst_element_link_many(
                src, appctx.queue1, preprocess_, pgie_,
                appctx.queue2, tracker, qt, appctx.nvanalytics,
                appctx.queue6, appctx.nvdslogger, appctx.tiler, appctx.queue3,
                appctx.nvvidconv, appctx.queue4, appctx.nvosd,
                appctx.queue5, NULL)) {
            g_printerr("PipelineTrafficcamnet link failed (no SGIEs). Exiting.\n");
        }
    }
}
