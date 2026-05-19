#include "stream_manager.hpp"
#include <iostream>
#include <cstring>
#include <hiredis/hiredis.h>
#include "gst-nvmultiurisrcbincreator.h"

extern "C" {
#include <glib.h>
}

StreamManager::StreamManager(void* nvmultiurisrcbinCreator,
                             void* bincreator_lock,
                             unsigned int* source_id_counter,
                             const std::string& redis_url,
                             const std::string& commands_channel,
                             unsigned int max_batch_size)
    : bin_creator_(nvmultiurisrcbinCreator)
    , bin_lock_(bincreator_lock)
    , source_id_counter_(source_id_counter)
    , max_batch_size_(max_batch_size)
    , redis_url_(redis_url)
    , commands_channel_(commands_channel)
    , running_(false)
    , reload_analytics_cb_(nullptr)
    , reload_analytics_ctx_(nullptr) {
}

StreamManager::~StreamManager() {
    stop();
}

bool StreamManager::start() {
    running_ = true;
    listener_thread_ = std::thread(&StreamManager::command_listener_loop, this);
    return true;
}

void StreamManager::stop() {
    running_ = false;
    if (listener_thread_.joinable()) {
        listener_thread_.join();
    }
}

bool StreamManager::is_running() const {
    return running_;
}

void StreamManager::set_reload_analytics_cb(void (*cb)(void*), void* ctx) {
    reload_analytics_cb_ = cb;
    reload_analytics_ctx_ = ctx;
}

void StreamManager::command_listener_loop() {
    std::string host = "127.0.0.1";
    int port = 6379;
    std::string url = redis_url_;
    if (url.substr(0, 8) == "redis://") {
        url = url.substr(8);
    }
    size_t colon_pos = url.find(':');
    if (colon_pos != std::string::npos) {
        host = url.substr(0, colon_pos);
        port = std::stoi(url.substr(colon_pos + 1));
    } else {
        host = url;
    }
    struct timeval tv = {2, 0};
    redisContext* ctx = redisConnectWithTimeout(host.c_str(), port, tv);
    if (!ctx || ctx->err) {
        std::cerr << "StreamManager: failed to connect to Redis at "
                  << redis_url_ << ": "
                  << (ctx ? ctx->errstr : "allocation failed") << std::endl;
        if (ctx) redisFree(ctx);
        running_ = false;
        return;
    }

    redisReply* reply = (redisReply*)redisCommand(ctx, "SUBSCRIBE %s",
                                                   commands_channel_.c_str());
    if (reply) freeReplyObject(reply);

    std::cout << "StreamManager: subscribed to " << commands_channel_
              << ", listening for commands..." << std::endl;

    while (running_) {
        void* reply_ptr = nullptr;
        if (redisGetReply(ctx, &reply_ptr) != REDIS_OK) {
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            continue;
        }
        redisReply* r = (redisReply*)reply_ptr;
        if (!r) {
            continue;
        }

        if (r->type == REDIS_REPLY_ARRAY && r->elements >= 3) {
            std::string msg_data;
            if (r->element[2]->type == REDIS_REPLY_STRING) {
                msg_data.assign(r->element[2]->str, r->element[2]->len);
            }

            StreamCommand cmd;
            if (parse_command(msg_data, cmd)) {
                if (cmd.action == "add") {
                    handle_add_stream(cmd);
                } else if (cmd.action == "remove") {
                    handle_remove_stream(cmd);
                } else if (cmd.action == "reload_analytics") {
                    handle_reload_analytics();
                } else if (cmd.action == "quit") {
                    handle_quit();
                } else {
                    std::cerr << "StreamManager: unknown action: "
                              << cmd.action << std::endl;
                }
            }
        }
        freeReplyObject(r);
    }

    redisFree(ctx);
    std::cout << "StreamManager: command listener stopped" << std::endl;
}

bool StreamManager::parse_command(const std::string& json_msg, StreamCommand& cmd) {
    cmd.device_id = -1;
    cmd.action.clear();
    cmd.sensor_id.clear();
    cmd.uri.clear();

    size_t action_pos = json_msg.find("\"action\"");
    if (action_pos == std::string::npos) return false;

    size_t start = json_msg.find("\"", action_pos + 8);
    size_t end = json_msg.find("\"", start + 1);
    if (start == std::string::npos || end == std::string::npos) return false;
    cmd.action = json_msg.substr(start + 1, end - start - 1);

    auto parse_field = [&](const char* field, std::string& out) {
        std::string f_with_quotes = "\"" + std::string(field) + "\"";
        size_t pos = json_msg.find(f_with_quotes);
        if (pos == std::string::npos) return;
        size_t s = json_msg.find("\"", pos + f_with_quotes.size());
        size_t e = json_msg.find("\"", s + 1);
        if (s != std::string::npos && e != std::string::npos) {
            out = json_msg.substr(s + 1, e - s - 1);
        }
    };

    auto parse_int = [&](const char* field, int& out) {
        size_t pos = json_msg.find(field);
        if (pos == std::string::npos) return;
        size_t s = json_msg.find_first_of("0123456789", pos);
        size_t e = json_msg.find_first_not_of("0123456789", s);
        if (s != std::string::npos) {
            out = std::stoi(json_msg.substr(s, e - s));
        }
    };

    parse_int("device_id", cmd.device_id);
    parse_field("sensor_id", cmd.sensor_id);
    parse_field("uri", cmd.uri);

    return !cmd.action.empty() && cmd.device_id >= 0;
}

void StreamManager::handle_add_stream(const StreamCommand& cmd) {
    g_mutex_lock((GMutex*)bin_lock_);

    GstDsNvUriSrcConfig config;
    memset(&config, 0, sizeof(GstDsNvUriSrcConfig));
    config.uri = (gchar*)cmd.uri.c_str();
    config.sensorId = (gchar*)cmd.sensor_id.c_str();
    config.source_id = ++(*((guint*)source_id_counter_));

    gboolean ret = gst_nvmultiurisrcbincreator_add_source(
        bin_creator_, &config);

    if (ret) {
        std::cout << "StreamManager: added source sensor_id="
                  << cmd.sensor_id << " uri=" << cmd.uri << std::endl;
    } else {
        std::cerr << "StreamManager: failed to add sensor_id="
                  << cmd.sensor_id << std::endl;
    }

    gst_nvmultiurisrcbincreator_sync_children_states(bin_creator_);
    g_mutex_unlock((GMutex*)bin_lock_);
}

void StreamManager::handle_remove_stream(const StreamCommand& cmd) {
    g_mutex_lock((GMutex*)bin_lock_);

    const GstDsNvUriSrcConfig* src_config =
        gst_nvmultiurisrcbincreator_get_source_config(
            bin_creator_, cmd.uri.c_str(), cmd.sensor_id.c_str());

    if (src_config) {
        gboolean ret = gst_nvmultiurisrcbincreator_remove_source(
            bin_creator_, src_config->source_id);
        gst_nvmultiurisrcbincreator_src_config_free(
            (GstDsNvUriSrcConfig*)src_config);

        if (ret) {
            std::cout << "StreamManager: removed sensor_id="
                      << cmd.sensor_id << std::endl;
        }
        gst_nvmultiurisrcbincreator_sync_children_states(bin_creator_);
    } else {
        std::cerr << "StreamManager: source not found for sensor_id="
                  << cmd.sensor_id << std::endl;
    }

    g_mutex_unlock((GMutex*)bin_lock_);
}

void StreamManager::handle_reload_analytics() {
    if (reload_analytics_cb_) {
        reload_analytics_cb_(reload_analytics_ctx_);
        std::cout << "StreamManager: analytics config reloaded" << std::endl;
    }
}

void StreamManager::handle_quit() {
    std::cout << "StreamManager: quit command received" << std::endl;
    running_ = false;
}