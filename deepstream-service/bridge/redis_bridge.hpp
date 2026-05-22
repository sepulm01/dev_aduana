#ifndef REDIS_BRIDGE_HPP
#define REDIS_BRIDGE_HPP

#include <gst/gst.h>
#include <glib.h>
#include <gstnvdsmeta.h>
#include <nvbufsurface.h>
#include <string>
#include <map>
#include <vector>

extern "C" {
#include <hiredis/hiredis.h>
#include "nvds_obj_encode.h"
}

struct DetectionObject {
    guint64 object_id;
    gint class_id;
    std::string class_label;
    gfloat confidence;
    gfloat left;
    gfloat top;
    gfloat width;
    gfloat height;
    std::string label;
};

struct FaceEmbedding {
    guint64 object_id;
    std::vector<float> landmarks;
    std::vector<float> embedding;
    float quality_score;
};

class RedisBridge {
public:
    RedisBridge(const std::string& redis_url, void* appctx);
    ~RedisBridge();

    bool start();
    void stop();
    void set_labels(const std::map<int, std::string>& labels);
    void set_rest_port(int port);
    void set_crop_socket(const std::string& host, int port);

    static GstPadProbeReturn analytics_pad_probe(GstPad* pad, GstPadProbeInfo* info,
                                                  gpointer user_data);

private:
    static void* subscriber_thread_func(void* arg);
    void subscriber_thread();
    void handle_command(const std::string& json_str);
    void publish_detection_json(const std::string& json_str);
    std::string make_detection_json(int device_id, int source_id, guint64 frame_num,
                                     const std::vector<DetectionObject>& objects,
                                     const std::vector<FaceEmbedding>& face_embeddings);

    float compute_quality_score(const std::vector<float>& landmarks,
                                float bbox_left, float bbox_top,
                                float bbox_width, float bbox_height);
    bool extract_sgie_tensor_data(NvDsObjectMeta* parent_obj,
                                  guint unique_id, std::vector<float>& data);

    bool connect_crop_socket();
    void close_crop_socket();
    bool send_face_crop(NvDsObjectMeta* obj_meta, NvDsFrameMeta* frame_meta,
                        int device_id, const FaceEmbedding& fe);

    std::string redis_url_;
    void* appctx_;
    redisContext* pub_ctx_;
    redisContext* sub_ctx_;
    GThread* subscriber_thread_;
    GMutex lock_;
    gboolean running_;
    std::map<int, std::string> labels_;
    std::map<int, int> source_to_device_;
    guint64 last_flush_time_;
    int rest_port_;

    NvDsObjEncCtxHandle enc_ctx_;
    int crop_sock_fd_;
    std::string crop_sock_host_;
    int crop_sock_port_;
    bool crop_sock_connected_;
};

#endif
