#ifndef STREAM_MANAGER_HPP
#define STREAM_MANAGER_HPP

#include <string>
#include <thread>
#include <mutex>

struct StreamCommand {
    std::string action;
    int device_id;
    std::string sensor_id;
    std::string uri;
};

class StreamManager {
public:
    StreamManager(void* nvmultiurisrcbinCreator,
                  void* bincreator_lock,
                  unsigned int* source_id_counter,
                  const std::string& redis_url,
                  const std::string& commands_channel,
                  unsigned int max_batch_size);

    ~StreamManager();

    bool start();
    void stop();
    bool is_running() const;

    void set_reload_analytics_cb(void (*cb)(void*), void* ctx);

private:
    void command_listener_loop();
    bool parse_command(const std::string& json_msg, StreamCommand& cmd);
    void handle_add_stream(const StreamCommand& cmd);
    void handle_remove_stream(const StreamCommand& cmd);
    void handle_reload_analytics();
    void handle_quit();

    void* bin_creator_;
    void* bin_lock_;
    unsigned int* source_id_counter_;
    unsigned int max_batch_size_;

    std::string redis_url_;
    std::string commands_channel_;
    bool running_;
    std::thread listener_thread_;

    void (*reload_analytics_cb_)(void*);
    void* reload_analytics_ctx_;
};

#endif