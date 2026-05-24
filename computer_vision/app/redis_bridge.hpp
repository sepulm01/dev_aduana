#ifndef REDIS_BRIDGE_HPP
#define REDIS_BRIDGE_HPP

#include <gst/gst.h>
#include <glib.h>
#include <gstnvdsmeta.h>
#include <string>
#include <map>
#include <vector>

extern "C" {
#include <hiredis/hiredis.h>
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

class RedisBridge {
public:
    RedisBridge(const std::string& redis_url, void* appctx);
    ~RedisBridge();

    bool start();
    void stop();
    void set_labels(const std::map<int, std::string>& labels);
    void set_rest_port(int port);

    static GstPadProbeReturn analytics_pad_probe(GstPad* pad, GstPadProbeInfo* info,
                                                  gpointer user_data);

private:
    static void* subscriber_thread_func(void* arg);
    void subscriber_thread();
    void handle_command(const std::string& json_str);
    void publish_detection_json(const std::string& json_str);
    std::string make_detection_json(int device_id, int source_id, guint64 frame_num,
                                     const std::vector<DetectionObject>& objects);

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
};

#endif
