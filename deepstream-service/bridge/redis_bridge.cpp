/*
 * Redis Bridge Implementation
 */

#include "redis_bridge.hpp"
#include "nvds_rest_server.h"
#include "gstnvdsmeta.h"
#include <sstream>
#include <cstring>
#include <cstdlib>
#include <ctime>

extern "C" {
#include <hiredis/hiredis.h>
}

#include "gst-nvmultiurisrcbincreator.h"
#include "nvds_analytics_meta.h"
#include "nvds_appctx_server.h"

static std::map<int, int> source_to_device;

static int get_device_id_from_source_id(int source_id) {
    auto it = source_to_device.find(source_id);
    if (it != source_to_device.end()) return it->second;
    return source_id;
}

RedisBridge::RedisBridge(const std::string& redis_url,
                         const std::string& commands_channel,
                         const std::string& events_prefix,
                         void* appctx)
    : redis_url_(redis_url),
      commands_channel_(commands_channel),
      events_prefix_(events_prefix),
      appctx_(appctx),
      redis_ctx_(nullptr),
      subscriber_thread_(nullptr),
      running_(FALSE),
      last_heartbeat_frame_(0) {
    g_mutex_init(&redis_lock_);
}

RedisBridge::~RedisBridge() {
    stop();
    g_mutex_clear(&redis_lock_);
}

bool RedisBridge::start() {
    redis_ctx_ = redisConnect(redis_url_.c_str(), 6379);
    if (!redis_ctx_ || redis_ctx_->err) {
        if (redis_ctx_) {
            g_printerr("Redis connection error: %s\n", redis_ctx_->errstr);
            redisFree(redis_ctx_);
            redis_ctx_ = nullptr;
        }
        return false;
    }
    g_print("[RedisBridge] Connected to Redis at %s\n", redis_url_.c_str());

    running_ = TRUE;
    GThread* th = g_thread_new("redis-subscriber",
                               subscriber_thread_func, this);
    if (!th) {
        g_printerr("Failed to create Redis subscriber thread\n");
        running_ = FALSE;
        return false;
    }
    subscriber_thread_ = th;
    return true;
}

void RedisBridge::stop() {
    g_mutex_lock(&redis_lock_);
    running_ = FALSE;
    g_mutex_unlock(&redis_lock_);

    if (redis_ctx_) {
        redisFree(redis_ctx_);
        redis_ctx_ = nullptr;
    }

    if (subscriber_thread_) {
        g_thread_join(subscriber_thread_);
        subscriber_thread_ = nullptr;
    }
    g_print("[RedisBridge] Stopped\n");
}

void RedisBridge::set_labels(const std::map<int, std::string>& labels) {
    g_mutex_lock(&redis_lock_);
    labels_ = labels;
    g_mutex_unlock(&redis_lock_);
}

void* RedisBridge::subscriber_thread_func(void* arg) {
    RedisBridge* bridge = static_cast<RedisBridge*>(arg);
    bridge->subscriber_thread();
    return nullptr;
}

void RedisBridge::subscriber_thread() {
    redisReply* reply = (redisReply*)redisCommand(redis_ctx_, "SUBSCRIBE %s", commands_channel_.c_str());
    if (!reply) {
        g_printerr("[RedisBridge] Failed to subscribe to %s\n", commands_channel_.c_str());
        return;
    }
    if (reply->type == REDIS_REPLY_ERROR) {
        g_printerr("[RedisBridge] Subscribe error: %s\n", reply->str);
        freeReplyObject(reply);
        return;
    }
    freeReplyObject(reply);
    g_print("[RedisBridge] Subscribed to %s\n", commands_channel_.c_str());

    while (running_) {
        g_mutex_lock(&redis_lock_);
        if (!running_) {
            g_mutex_unlock(&redis_lock_);
            break;
        }
        g_mutex_unlock(&redis_lock_);

        redisReply* msg = nullptr;
        int ret = redisGetReply(redis_ctx_, (void**)&msg);
        if (ret != REDIS_OK || !msg) {
            g_usleep(100000);
            continue;
        }

        if (msg->type == REDIS_REPLY_ARRAY && msg->elements >= 3) {
            const char* channel = msg->element[1]->str;
            const char* payload = msg->element[2]->str;
            g_print("[RedisBridge] Received on %s: %s\n", channel, payload);
            handle_command(payload);
        }
        freeReplyObject(msg);
    }
}

bool RedisBridge::handle_command(const std::string& json_str) {
    AppCtx* appctx = static_cast<AppCtx*>(appctx_);
    if (!appctx) return false;

    std::string action;
    int device_id = 0;
    std::string camera_url, camera_name, camera_id;

    std::istringstream json(json_str);
    std::string token;
    bool in_string = false;
    std::string current_key, current_value;
    char prev_char = 0;

    for (size_t i = 0; i < json_str.size(); ++i) {
        char c = json_str[i];
        if (c == '"' && prev_char != '\\') {
            in_string = !in_string;
        } else if (!in_string && c == ':') {
            current_key = current_value;
            current_value.clear();
        } else if (!in_string && (c == ',' || c == '}')) {
            if (current_key == "action") action = current_value;
            else if (current_key == "device_id") device_id = atoi(current_value.c_str());
            else if (current_key == "camera_url") camera_url = current_value;
            else if (current_key == "camera_name") camera_name = current_value;
            else if (current_key == "camera_id") camera_id = current_value;
            current_value.clear();
            current_key.clear();
            if (c == '}') break;
        } else if (!in_string && c != ' ' && c != '\n' && c != '\t') {
            current_value += c;
        }
        prev_char = c;
    }

    g_print("[RedisBridge] action=%s device_id=%d\n", action.c_str(), device_id);

    if (action == "add" || action == "camera_add") {
        if (camera_url.empty()) {
            g_printerr("[RedisBridge] camera_url missing\n");
            return false;
        }
        g_mutex_lock(&appctx->bincreator_lock);

        GstDsNvUriSrcConfig** sourceConfigs = nullptr;
        guint numSourceConfigs = 0;
        if (gst_nvmultiurisrcbincreator_get_active_sources_list(appctx->nvmultiurisrcbinCreator,
                &numSourceConfigs, &sourceConfigs)) {
            gst_nvmultiurisrcbincreator_src_config_list_free(appctx->nvmultiurisrcbinCreator,
                                                             numSourceConfigs, sourceConfigs);
            if (numSourceConfigs >= appctx->muxConfig.maxBatchSize) {
                g_printerr("[RedisBridge] Max sources reached (%d)\n", numSourceConfigs);
                g_mutex_unlock(&appctx->bincreator_lock);
                return false;
            }
        }

        std::string sid = camera_id.empty() ? std::to_string(device_id) : camera_id;
        GstDsNvUriSrcConfig* existing = gst_nvmultiurisrcbincreator_get_source_config_by_sensorid(
            appctx->nvmultiurisrcbinCreator, sid.c_str());
        if (existing) {
            gst_nvmultiurisrcbincreator_src_config_free(existing);
            g_printerr("[RedisBridge] Source %s already exists\n", sid.c_str());
            g_mutex_unlock(&appctx->bincreator_lock);
            return false;
        }

        appctx->config.uri = (gchar*)camera_url.c_str();
        appctx->config.sensorId = (gchar*)sid.c_str();
        appctx->config.sensorName = (gchar*)(camera_name.empty() ? sid.c_str() : camera_name.c_str());
        appctx->config.source_id = ++appctx->sourceIdCounter;

        int src_id = appctx->config.source_id;
        source_to_device[src_id] = device_id;

        g_print("[RedisBridge] Adding source: id=%d url=%s\n", src_id, camera_url.c_str());
        gboolean ret = gst_nvmultiurisrcbincreator_add_source(appctx->nvmultiurisrcbinCreator,
                                                              &appctx->config);
        gst_nvmultiurisrcbincreator_sync_children_states(appctx->nvmultiurisrcbinCreator);

        appctx->config.uri = nullptr;
        appctx->config.sensorId = nullptr;
        g_mutex_unlock(&appctx->bincreator_lock);

        if (ret) {
            publish_analytics_event(device_id, "device_added",
                                    {{"source_id", std::to_string(src_id)}});
        }
        return ret;
    }
    else if (action == "remove" || action == "camera_remove") {
        g_mutex_lock(&appctx->bincreator_lock);
        std::string sid = camera_id.empty() ? std::to_string(device_id) : camera_id;
        g_print("[RedisBridge] Removing source: sensor_id=%s\n", sid.c_str());
        GstDsNvUriSrcConfig* sc = gst_nvmultiurisrcbincreator_get_source_config_by_sensorid(
            appctx->nvmultiurisrcbinCreator, sid.c_str());
        gboolean ret = FALSE;
        if (sc) {
            guint src_id = sc->source_id;
            ret = gst_nvmultiurisrcbincreator_remove_source(appctx->nvmultiurisrcbinCreator, src_id);
            gst_nvmultiurisrcbincreator_src_config_free(sc);
        }
        gst_nvmultiurisrcbincreator_sync_children_states(appctx->nvmultiurisrcbinCreator);
        g_mutex_unlock(&appctx->bincreator_lock);
        return ret;
    }
    else if (action == "quit" || action == "app_quit") {
        g_print("[RedisBridge] Quit command received\n");
        GstEvent* eos_event = gst_event_new_eos();
        gst_pad_push_event(gst_nvmultiurisrcbincreator_get_source_pad(appctx->nvmultiurisrcbinCreator),
                           eos_event);
        return true;
    }

    return false;
}

std::string RedisBridge::make_analytics_json(int device_id, const char* action,
                                              const std::map<int, int>& obj_counts,
                                              int line_L1, int line_L2, int overcrowding) {
    std::ostringstream oss;
    oss << "{\"code\":\"AnalyticsSummary\",\"action\":\"" << action << "\",\"index\":0,"
        << "\"timestamp\":\"" << time(nullptr) << "\","
        << "\"data\":{\"device_id\":" << device_id << ",";

    oss << "\"object_counts\":{";
    bool first = true;
    g_mutex_lock(&redis_lock_);
    for (const auto& kv : obj_counts) {
        if (!first) oss << ",";
        first = false;
        auto it = labels_.find(kv.first);
        std::string label = (it != labels_.end()) ? it->second : std::to_string(kv.first);
        oss << "\"" << label << "\":" << kv.second;
    }
    g_mutex_unlock(&redis_lock_);

    oss << "},\"line_crossings\":{\"L1\":" << line_L1 << ",\"L2\":" << line_L2 << "},"
        << "\"overcrowding_count\":" << overcrowding << "}}";
    return oss.str();
}

void RedisBridge::publish_analytics_event(int device_id, const std::string& event_type,
                                          const std::map<std::string, std::string>& data) {
    if (!redis_ctx_) return;

    std::ostringstream oss;
    oss << "{\"code\":\"" << event_type << "\",\"device_id\":" << device_id;
    for (const auto& kv : data) {
        oss << ",\"" << kv.first << "\":\"" << kv.second << "\"";
    }
    oss << ",\"timestamp\":" << time(nullptr) << "}";

    std::string channel = events_prefix_ + ":" + std::to_string(device_id) + ":events";
    redisReply* reply = (redisReply*)redisCommand(redis_ctx_, "PUBLISH %s %s",
                                                  channel.c_str(), oss.str().c_str());
    if (reply) {
        g_print("[RedisBridge] Published to %s: %s\n", channel.c_str(), oss.str().c_str());
        freeReplyObject(reply);
    }
}

GstPadProbeReturn RedisBridge::analytics_pad_probe(GstPad* pad, GstPadProbeInfo* info, gpointer user_data) {
    RedisBridge* bridge = static_cast<RedisBridge*>(user_data);
    GstBuffer* buf = GST_BUFFER(info->data);

    NvDsBatchMeta* batch_meta = gst_buffer_get_nvds_batch_meta(buf);
    if (!batch_meta) return GST_PAD_PROBE_OK;

    static guint64 last_flush_time = 0;
    guint64 current_time = g_get_monotonic_time();

    std::map<int, AnalyticsObjData> accum;
    std::map<int, int> device_frame_count;

    for (NvDsMetaList* l_frame = batch_meta->frame_meta_list; l_frame; l_frame = l_frame->next) {
        NvDsFrameMeta* frame_meta = (NvDsFrameMeta*)l_frame->data;
        int source_id = frame_meta->source_id;
        int device_id = get_device_id_from_source_id(source_id);

        AnalyticsObjData& ad = accum[device_id];
        device_frame_count[device_id] = frame_meta->frame_num;

        for (NvDsMetaList* l_obj = frame_meta->obj_meta_list; l_obj; l_obj = l_obj->next) {
            NvDsObjectMeta* obj_meta = (NvDsObjectMeta*)l_obj->data;
            ad.obj_counts[obj_meta->class_id]++;
            ad.has_data = true;
        }

        NvDsUserMetaList* uuser = frame_meta->frame_user_meta_list;
        while (uuser) {
            NvDsUserMeta* user_meta = (NvDsUserMeta*)uuser->data;
            if (user_meta->base_meta.meta_type == NVDS_USER_FRAME_META_NVDSANALYTICS) {
                NvDsAnalyticsFrameMeta* analytics_meta =
                    (NvDsAnalyticsFrameMeta*)user_meta->user_meta_data;
                if (analytics_meta) {
                    for (const auto& kv : analytics_meta->objLCCurrCnt) {
                        if (kv.second > 0) {
                            ad.line_crossing_L1 += kv.second;
                            ad.has_data = true;
                        }
                    }
                    for (const auto& kv : analytics_meta->objInROIcnt) {
                        if (kv.second > 0) {
                            ad.overcrowding_count += kv.second;
                            ad.has_data = true;
                        }
                    }
                }
            }
            uuser = uuser->next;
        }
    }

    if (current_time - last_flush_time >= 1000000) {
        for (auto& kv : accum) {
            int device_id = kv.first;
            AnalyticsObjData& ad = kv.second;
            if (!ad.has_data) continue;

            std::string json = bridge->make_analytics_json(
                device_id, "summary", ad.obj_counts,
                ad.line_crossing_L1, ad.line_crossing_L2, ad.overcrowding_count);

            if (bridge->redis_ctx_) {
                std::string channel = bridge->events_prefix_ + ":" + std::to_string(device_id) + ":events";
                redisReply* reply = (redisReply*)redisCommand(
                    bridge->redis_ctx_, "PUBLISH %s %s", channel.c_str(), json.c_str());
                if (reply) freeReplyObject(reply);
            }

            g_print("[AnalyticsPadProbe] device=%d objects=%zu LC=%d OC=%d\n",
                    device_id, ad.obj_counts.size(),
                    ad.line_crossing_L1, ad.overcrowding_count);

            ad.obj_counts.clear();
            ad.line_crossing_L1 = 0;
            ad.line_crossing_L2 = 0;
            ad.overcrowding_count = 0;
            ad.has_data = false;
        }
        last_flush_time = current_time;
    }

    return GST_PAD_PROBE_OK;
}