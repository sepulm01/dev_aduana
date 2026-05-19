#include "analytics_probe.hpp"
#include "gstnvdsmeta.h"
#include "nvds_analytics_meta.h"
#include <gst/gst.h>
#include <stdio.h>
#include <sstream>
#include <iomanip>

AnalyticsProbe::AnalyticsProbe(RedisPublisher* redis_publisher)
    : redis_pub_(redis_publisher)
    , frame_count_(0)
    , last_heartbeat_frame_(0)
    , heartbeat_interval_(300) {
}

void AnalyticsProbe::set_source_to_device_map(std::map<int, int> map) {
    source_to_device_ = std::move(map);
}

void AnalyticsProbe::set_labels(const std::map<int, std::string>& labels) {
    class_labels_ = labels;
}

std::string AnalyticsProbe::sanitize_json_str(const std::string& s) {
    std::ostringstream out;
    for (char c : s) {
        if (c == '"' || c == '\\') out << '\\';
        out << c;
    }
    return out.str();
}

std::string AnalyticsProbe::format_bbox(double left, double top,
                                         double width, double height) {
    std::ostringstream s;
    s << "{\"left\":" << std::fixed << std::setprecision(2) << left
      << ",\"top\":" << top
      << ",\"width\":" << width
      << ",\"height\":" << height << "}";
    return s.str();
}

GstPadProbeReturn AnalyticsProbe::on_nvdsanalytics_src_pad_buffer(GstPad* pad, GstPadProbeInfo* info) {
    GstBuffer* buf = (GstBuffer*)info->data;
    NvDsBatchMeta* batch_meta = gst_buffer_get_nvds_batch_meta(buf);
    if (!batch_meta) return GST_PAD_PROBE_OK;

    frame_count_++;

    for (NvDsMetaList* l_frame = batch_meta->frame_meta_list; l_frame;
         l_frame = l_frame->next) {
        NvDsFrameMeta* frame = (NvDsFrameMeta*)l_frame->data;
        int source_id = (int)frame->source_id;
        int device_id = source_id;
        auto it = source_to_device_.find(source_id);
        if (it != source_to_device_.end()) {
            device_id = it->second;
        }

        for (NvDsMetaList* l_user = frame->frame_user_meta_list; l_user;
             l_user = l_user->next) {
            NvDsUserMeta* user_meta = (NvDsUserMeta*)l_user->data;
            if (user_meta->base_meta.meta_type != NVDS_USER_FRAME_META_NVDSANALYTICS)
                continue;

            NvDsAnalyticsFrameMeta* analytics =
                (NvDsAnalyticsFrameMeta*)user_meta->user_meta_data;

            for (auto& kv : analytics->objLCCurrCnt) {
                if (kv.second > 0) {
                    std::string line_name = kv.first;
                    int cum_count = 0;
                    auto cit = analytics->objLCCumCnt.find(kv.first);
                    if (cit != analytics->objLCCumCnt.end()) cum_count = cit->second;

                    std::ostringstream event_data;
                    event_data << "{\"source\":" << source_id
                               << ",\"frame_num\":" << frame->frame_num
                               << ",\"line_name\":\"" << sanitize_json_str(line_name) << "\""
                               << ",\"line_curr_count\":" << kv.second
                               << ",\"line_cum_count\":" << cum_count
                               << ",\"device_id\":" << device_id << "}";

                    redis_pub_->publish_device_event(device_id, "LineCrossing",
                                                     "Crossing", 0,
                                                     event_data.str());
                }
            }

            for (auto& kv : analytics->ocStatus) {
                if (kv.second) {
                    std::ostringstream event_data;
                    event_data << "{\"source\":" << source_id
                               << ",\"frame_num\":" << frame->frame_num
                               << ",\"roi_name\":\"" << sanitize_json_str(kv.first) << "\""
                               << ",\"device_id\":" << device_id << "}";

                    redis_pub_->publish_device_event(device_id, "ROIOvercrowding",
                                                     "Overcrowding", 0,
                                                     event_data.str());
                }
            }
        }

        int vehicle_count = 0;
        int person_count = 0;

        for (NvDsMetaList* l_obj = frame->obj_meta_list; l_obj;
             l_obj = l_obj->next) {
            NvDsObjectMeta* obj = (NvDsObjectMeta*)l_obj->data;

            if (obj->class_id == 0) vehicle_count++;
            if (obj->class_id == 2) person_count++;

            std::string class_label = "unknown";
            auto cit = class_labels_.find((int)obj->class_id);
            if (cit != class_labels_.end()) class_label = cit->second;

            std::string obj_dir_status;
            for (NvDsMetaList* l_um = obj->obj_user_meta_list; l_um;
                 l_um = l_um->next) {
                NvDsUserMeta* um = (NvDsUserMeta*)l_um->data;
                if (um->base_meta.meta_type == NVDS_USER_OBJ_META_NVDSANALYTICS) {
                    NvDsAnalyticsObjInfo* ai =
                        (NvDsAnalyticsObjInfo*)um->user_meta_data;
                    if (ai && !ai->dirStatus.empty()) {
                        obj_dir_status = ai->dirStatus;
                    }
                }
            }

            std::ostringstream det_data;
            det_data << "{\"source\":" << source_id
                     << ",\"frame_num\":" << frame->frame_num
                     << ",\"object_id\":" << obj->object_id
                     << ",\"class_id\":" << obj->class_id
                     << ",\"class_label\":\"" << sanitize_json_str(class_label) << "\""
                     << ",\"confidence\":" << std::fixed << std::setprecision(2) << obj->confidence
                     << ",\"bbox\":" << format_bbox(obj->rect_params.left,
                                                    obj->rect_params.top,
                                                    obj->rect_params.width,
                                                    obj->rect_params.height);
            if (!obj_dir_status.empty()) {
                det_data << ",\"dir_status\":\"" << sanitize_json_str(obj_dir_status) << "\"";
            }
            det_data << ",\"device_id\":" << device_id << "}";

            redis_pub_->publish_device_event(device_id, "DeepStreamDetection",
                                             "Detected", 0, det_data.str());
        }
    }

    if (frame_count_ - last_heartbeat_frame_ >= heartbeat_interval_) {
        last_heartbeat_frame_ = frame_count_;
        redis_pub_->publish_heartbeat(0, frame_count_, 0.0,
                                       (int)source_to_device_.size());
    }

    return GST_PAD_PROBE_OK;
}

GstPadProbeReturn
analytics_src_pad_buffer_probe(GstPad* pad,
                                GstPadProbeInfo* info,
                                gpointer user_data) {
    AnalyticsProbe* probe = (AnalyticsProbe*)user_data;
    probe->on_nvdsanalytics_src_pad_buffer(pad, info);
    return GST_PAD_PROBE_OK;
}