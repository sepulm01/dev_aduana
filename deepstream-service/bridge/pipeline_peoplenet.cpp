#include "pipeline.h"

void PipelinePeoplenet::create_inference(AppCtx& appctx, guint batch_size)
{
    pgie_ = make("nvinfer", "primary-nvinference-engine");
    appctx.pgie = pgie_;
    gst_bin_add(GST_BIN(appctx.pipeline), pgie_);
}

void PipelinePeoplenet::link_inference(AppCtx& appctx, GstElement* src,
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

    if (!gst_element_link_many(
            src, appctx.queue1, preprocess_, pgie_, appctx.queue2,
            tracker, qt, appctx.nvanalytics, appctx.queue6,
            appctx.nvdslogger, appctx.tiler, appctx.queue3,
            appctx.nvvidconv, appctx.queue4, appctx.nvosd, appctx.queue5, NULL)) {
        g_printerr("PipelinePeoplenet link failed. Exiting.\n");
    }
}
