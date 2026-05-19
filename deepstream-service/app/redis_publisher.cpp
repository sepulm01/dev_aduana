#include "redis_publisher.hpp"
#include <iostream>
#include <sstream>
#include <iomanip>
#include <cstring>
#include <ctime>

RedisPublisher::RedisPublisher(const std::string& redis_url)
    : redis_url_(redis_url), ctx_(nullptr), connected_(false) {
}

RedisPublisher::~RedisPublisher() {
    disconnect();
}

bool RedisPublisher::connect() {
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
    ctx_ = redisConnectWithTimeout(host.c_str(), port, tv);
    if (ctx_ == nullptr) {
        std::cerr << "RedisPublisher: failed to allocate context" << std::endl;
        return false;
    }
    if (ctx_->err) {
        std::cerr << "RedisPublisher: connection error: " << ctx_->errstr << std::endl;
        redisFree(ctx_);
        ctx_ = nullptr;
        return false;
    }
    connected_ = true;
    std::cout << "RedisPublisher: connected to " << redis_url_ << std::endl;
    return true;
}

void RedisPublisher::disconnect() {
    if (ctx_) {
        redisFree(ctx_);
        ctx_ = nullptr;
        connected_ = false;
    }
}

void RedisPublisher::publish(const std::string& channel, const std::string& message) {
    if (!connected_ || !ctx_) return;
    redisReply* reply = (redisReply*)redisCommand(ctx_, "PUBLISH %s %s",
                                                   channel.c_str(), message.c_str());
    if (reply) {
        freeReplyObject(reply);
    }
}

std::string RedisPublisher::build_event_json(
        const std::string& code,
        const std::string& action,
        int index,
        const std::map<std::string, std::string>& data) {

    std::ostringstream out;
    out << "{";
    out << "\"code\":\"" << code << "\",";
    out << "\"action\":\"" << action << "\",";
    out << "\"index\":" << index << ",";

    time_t now = time(nullptr);
    char ts_buf[64];
    strftime(ts_buf, sizeof(ts_buf), "%Y-%m-%dT%H:%M:%SZ", gmtime(&now));
    out << "\"timestamp\":\"" << ts_buf << "\",";

    out << "\"data\":{";
    bool first = true;
    for (const auto& kv : data) {
        if (!first) out << ",";
        first = false;
        out << "\"" << kv.first << "\":" << kv.second;
    }
    out << "}}";
    return out.str();
}

void RedisPublisher::publish_device_event(int device_id, const std::string& code,
                                          const std::string& action, int index,
                                          const std::string& data_json) {
    if (!connected_) return;
    std::ostringstream channel;
    channel << "device:" << device_id << ":events";

    std::ostringstream msg;
    msg << "{";
    msg << "\"code\":\"" << code << "\",";
    msg << "\"action\":\"" << action << "\",";
    msg << "\"index\":" << index << ",";

    time_t now = time(nullptr);
    char ts_buf[64];
    strftime(ts_buf, sizeof(ts_buf), "%Y-%m-%dT%H:%M:%SZ", gmtime(&now));
    msg << "\"timestamp\":\"" << ts_buf << "\",";

    if (!data_json.empty() && data_json[0] == '{') {
        std::string data_content = data_json.substr(1, data_json.size() - 2);
        msg << "\"data\":" << data_json;
    } else {
        msg << "\"data\":{}";
    }
    msg << "}";

    publish(channel.str(), msg.str());
}

void RedisPublisher::publish_heartbeat(int device_id, int frame_num, double fps,
                                       int active_sources) {
    if (!connected_) return;
    std::ostringstream channel;
    channel << "device:" << device_id << ":events";

    std::ostringstream msg;
    msg << "{";
    msg << "\"code\":\"DeepStreamHeartbeat\",";
    msg << "\"action\":\"heartbeat\",";
    msg << "\"index\":0,";

    time_t now = time(nullptr);
    char ts_buf[64];
    strftime(ts_buf, sizeof(ts_buf), "%Y-%m-%dT%H:%M:%SZ", gmtime(&now));
    msg << "\"timestamp\":\"" << ts_buf << "\",";
    msg << "\"data\":{";
    msg << "\"frame_num\":" << frame_num << ",";
    msg << "\"fps\":" << std::fixed << std::setprecision(2) << fps << ",";
    msg << "\"active_sources\":" << active_sources;
    msg << "}}";

    publish(channel.str(), msg.str());
}