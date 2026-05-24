#ifndef PIPELINE_H
#define PIPELINE_H

#include <gst/gst.h>
#include "nvds_appctx_server.h"
#include "nvds_yml_parser.h"

class Pipeline {
public:
    Pipeline(const char* yml_path);
    virtual ~Pipeline();

    bool build(AppCtx& appctx);
    GstElement* source_bin(AppCtx& appctx) const;

    static Pipeline* create_from_env(const char* yml_path);

protected:
    virtual void create_inference(AppCtx& appctx, guint batch_size) = 0;
    virtual void link_inference(AppCtx& appctx, GstElement* src,
                                GstElement* tracker, GstElement* qt) = 0;

    bool parse_codec();
    bool create_source(AppCtx& appctx);
    bool create_encoder(AppCtx& appctx);
    bool create_common(AppCtx& appctx);
    bool create_tracker(AppCtx& appctx, GstElement*& tracker, GstElement*& qt);
    void configure_elements(AppCtx& appctx, guint batch_size);
    void add_common_to_bin(AppCtx& appctx);
    bool link_sink(AppCtx& appctx);

    GstElement* make(const char* factory, const char* name);

    const char* yml_path_;

    gboolean enc_enable_;
    NvDsYamlCodecStatus codec_status_;

    GstElement* preprocess_;
    GstElement* pgie_;
    GstElement* sgie0_;
    GstElement* sgie1_;
};

class PipelineYolo : public Pipeline {
public:
    PipelineYolo(const char* yml_path) : Pipeline(yml_path) {}
protected:
    void create_inference(AppCtx& appctx, guint batch_size) override;
    void link_inference(AppCtx& appctx, GstElement* src,
                        GstElement* tracker, GstElement* qt) override;
};

class PipelinePeoplenet : public Pipeline {
public:
    PipelinePeoplenet(const char* yml_path) : Pipeline(yml_path) {}
protected:
    void create_inference(AppCtx& appctx, guint batch_size) override;
    void link_inference(AppCtx& appctx, GstElement* src,
                        GstElement* tracker, GstElement* qt) override;
};

class PipelineFacedetect : public Pipeline {
public:
    PipelineFacedetect(const char* yml_path) : Pipeline(yml_path) {}
protected:
    void create_inference(AppCtx& appctx, guint batch_size) override;
    void link_inference(AppCtx& appctx, GstElement* src,
                        GstElement* tracker, GstElement* qt) override;
};

class PipelineTrafficcamnet : public Pipeline {
public:
    PipelineTrafficcamnet(const char* yml_path) : Pipeline(yml_path) {}
protected:
    void create_inference(AppCtx& appctx, guint batch_size) override;
    void link_inference(AppCtx& appctx, GstElement* src,
                        GstElement* tracker, GstElement* qt) override;
};

#endif
