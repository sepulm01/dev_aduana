/*
 * Redis Bridge for DeepStream Server App
 *
 * Subscribes to deepstream:commands channel and translates Redis messages
 * into stream add/remove calls on the nvmultiurisrcbincreator.
 * Also publishes analytics events to device:{id}:events.
 */

#ifndef REDIS_BRIDGE_HPP
#define REDIS_BRIDGE_HPP

#include <gst/gst.h>
#include <glib.h>
#include <string>
#include <map>

extern "C" {
#include <hiredis/hiredis.h>
}

#define MAX_DISPLAY_LEN 64

struct AnalyticsObjData {
    std::map<int, int> obj_counts;
    int line_crossing_L1 = 0;
    int line_crossing_L2 = 0;
    int overcrowding_count = 0;
    bool has_data = false;
};

class RedisBridge {
public:
    RedisBridge(const std::string& redis_url,
                const std::string& commands_channel,
                const std::string& events_prefix,
                void* appctx);
    ~RedisBridge();

    bool start();
    void stop();
    void set_labels(const std::map<int, std::string>& labels);

    static GstPadProbeReturn analytics_pad_probe(GstPad* pad, GstPadProbeInfo* info, gpointer user_data);

private:
    static void* subscriber_thread_func(void* arg);
    void subscriber_thread();
    bool handle_command(const std::string& json_str);
    void publish_analytics_event(int device_id, const std::string& event_type,
                                  const std::map<std::string, std::string>& data);
    std::string make_analytics_json(int device_id, const char* action,
                                     const std::map<int, int>& obj_counts,
                                     int line_L1, int line_L2, int overcrowding);

    std::string redis_url_;
    std::string commands_channel_;
    std::string events_prefix_;
    void* appctx_;
    redisContext* redis_ctx_;
    GThread* subscriber_thread_;
    GMutex redis_lock_;
    gboolean running_;
    std::map<int, std::string> labels_;
    std::map<int, AnalyticsObjData> device_accumulator_;
    guint last_heartbeat_frame_;
};

#endif