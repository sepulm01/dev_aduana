#ifndef REDIS_PUBLISHER_HPP
#define REDIS_PUBLISHER_HPP

#include <string>
#include <vector>
#include <map>
#include <hiredis/hiredis.h>

class RedisPublisher {
public:
    RedisPublisher(const std::string& redis_url);
    ~RedisPublisher();

    bool connect();
    void disconnect();

    void publish(const std::string& channel, const std::string& message);
    void publish_device_event(int device_id, const std::string& code,
                               const std::string& action, int index,
                               const std::string& data_json);
    void publish_heartbeat(int device_id, int frame_num, double fps,
                           int active_sources);

private:
    std::string build_event_json(const std::string& code, const std::string& action,
                                  int index, const std::map<std::string, std::string>& data);

    std::string redis_url_;
    redisContext* ctx_;
    bool connected_;
};

#endif