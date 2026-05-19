#ifndef ANALYTICS_PROBE_HPP
#define ANALYTICS_PROBE_HPP

#include <gst/gst.h>
#include "redis_publisher.hpp"
#include <map>
#include <string>

class AnalyticsProbe {
public:
    AnalyticsProbe(RedisPublisher* redis_publisher);

    void set_source_to_device_map(std::map<int, int> map);
    void set_labels(const std::map<int, std::string>& labels);

    GstPadProbeReturn on_nvdsanalytics_src_pad_buffer(GstPad* pad, GstPadProbeInfo* info);

private:
    std::string format_bbox(double left, double top, double width, double height);
    std::string sanitize_json_str(const std::string& s);

    RedisPublisher* redis_pub_;
    std::map<int, int> source_to_device_;
    std::map<int, std::string> class_labels_;
    unsigned int frame_count_;
    unsigned int last_heartbeat_frame_;
    unsigned int heartbeat_interval_;
};

GstPadProbeReturn analytics_src_pad_buffer_probe(GstPad* pad, GstPadProbeInfo* info, gpointer user_data);

#endif