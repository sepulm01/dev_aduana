#include "redis_bridge.hpp"
#include "gstnvdsmeta.h"
#include "gstnvdsinfer.h"
#include "nvds_analytics_meta.h"
#include "nvbufsurface.h"
#include <sstream>
#include <ctime>
#include <cstring>
#include <cstdlib>
#include <curl/curl.h>
#include <json/json.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <netdb.h>
#include <unistd.h>

extern "C" {
#include <hiredis/hiredis.h>
}

#define FLUSH_INTERVAL_US 1000000

#pragma pack(push, 1)
struct FaceCropPacket {
    uint32_t device_id;
    uint64_t object_id;
    float quality_score;
    float bbox_left;
    float bbox_top;
    float bbox_width;
    float bbox_height;
    uint64_t timestamp_ms;
};
#pragma pack(pop)

static const char END_MARKER[] = "END!";

RedisBridge::RedisBridge(const std::string& redis_url, void* appctx)
    : redis_url_(redis_url),
      appctx_(appctx),
      pub_ctx_(nullptr),
      sub_ctx_(nullptr),
      subscriber_thread_(nullptr),
      running_(FALSE),
      last_flush_time_(0),
      rest_port_(9000),
      enc_ctx_(nullptr),
      crop_sock_fd_(-1),
      crop_sock_host_("face-receiver"),
      crop_sock_port_(12348),
      crop_sock_connected_(false)
{
    g_mutex_init(&lock_);
}

RedisBridge::~RedisBridge()
{
    stop();
    g_mutex_clear(&lock_);
}

void RedisBridge::set_labels(const std::map<int, std::string>& labels)
{
    g_mutex_lock(&lock_);
    labels_ = labels;
    g_mutex_unlock(&lock_);
}

void RedisBridge::set_rest_port(int port)
{
    rest_port_ = port;
}

void RedisBridge::set_crop_socket(const std::string& host, int port)
{
    crop_sock_host_ = host;
    crop_sock_port_ = port;
}

bool RedisBridge::connect_crop_socket()
{
    crop_sock_fd_ = socket(AF_INET, SOCK_STREAM, 0);
    if (crop_sock_fd_ < 0) {
        g_printerr("[RedisBridge] socket() failed\n");
        return false;
    }

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(crop_sock_port_);
    addr.sin_addr.s_addr = inet_addr(crop_sock_host_.c_str());

    if (addr.sin_addr.s_addr == INADDR_NONE) {
        struct hostent* he = gethostbyname(crop_sock_host_.c_str());
        if (!he) {
            g_printerr("[RedisBridge] DNS lookup failed for %s\n",
                       crop_sock_host_.c_str());
            close(crop_sock_fd_);
            crop_sock_fd_ = -1;
            return false;
        }
        memcpy(&addr.sin_addr, he->h_addr, he->h_length);
    }

    int ret = connect(crop_sock_fd_, (struct sockaddr*)&addr, sizeof(addr));
    if (ret < 0) {
        g_printerr("[RedisBridge] Crop socket connect to %s:%d failed\n",
                   crop_sock_host_.c_str(), crop_sock_port_);
        close(crop_sock_fd_);
        crop_sock_fd_ = -1;
        return false;
    }

    crop_sock_connected_ = true;
    g_print("[RedisBridge] Crop socket connected to %s:%d\n",
            crop_sock_host_.c_str(), crop_sock_port_);
    return true;
}

void RedisBridge::close_crop_socket()
{
    crop_sock_connected_ = false;
    if (crop_sock_fd_ >= 0) {
        shutdown(crop_sock_fd_, SHUT_RDWR);
        close(crop_sock_fd_);
        crop_sock_fd_ = -1;
    }
}

bool RedisBridge::send_face_crop(NvDsObjectMeta* obj_meta, NvDsFrameMeta* frame_meta,
                                  int device_id, const FaceEmbedding& fe)
{
    if (!crop_sock_connected_ || crop_sock_fd_ < 0) return false;

    for (NvDsMetaList* l_user = obj_meta->obj_user_meta_list; l_user;
         l_user = l_user->next) {
        NvDsUserMeta* user_meta = (NvDsUserMeta*)l_user->data;
        if (!user_meta) continue;
        if (user_meta->base_meta.meta_type != NVDS_CROP_IMAGE_META)
            continue;

        NvDsObjEncOutParams* enc = (NvDsObjEncOutParams*)user_meta->user_meta_data;
        if (!enc || !enc->outBuffer || enc->outLen == 0) continue;

        FaceCropPacket pkt;
        pkt.device_id = (uint32_t)device_id;
        pkt.object_id = obj_meta->object_id;
        pkt.quality_score = fe.quality_score;
        pkt.bbox_left = obj_meta->detector_bbox_info.org_bbox_coords.left / 1920.0f;
        pkt.bbox_top = obj_meta->detector_bbox_info.org_bbox_coords.top / 1080.0f;
        pkt.bbox_width = obj_meta->detector_bbox_info.org_bbox_coords.width / 1920.0f;
        pkt.bbox_height = obj_meta->detector_bbox_info.org_bbox_coords.height / 1080.0f;
        pkt.timestamp_ms = (uint64_t)(time(nullptr) * 1000LL);

        ssize_t sent;

        sent = send(crop_sock_fd_, enc->outBuffer, enc->outLen, MSG_NOSIGNAL);
        if (sent < 0) {
            g_printerr("[RedisBridge] Crop JPEG send failed\n");
            close_crop_socket();
            return false;
        }

        sent = send(crop_sock_fd_, END_MARKER, strlen(END_MARKER), MSG_NOSIGNAL);
        if (sent < 0) {
            close_crop_socket();
            return false;
        }

        sent = send(crop_sock_fd_, &pkt, sizeof(pkt), MSG_NOSIGNAL);
        if (sent < 0) {
            close_crop_socket();
            return false;
        }

        if (!fe.embedding.empty()) {
            sent = send(crop_sock_fd_, fe.embedding.data(),
                        fe.embedding.size() * sizeof(float), MSG_NOSIGNAL);
            if (sent < 0) {
                close_crop_socket();
                return false;
            }
        } else {
            float zero[512] = {};
            send(crop_sock_fd_, zero, sizeof(zero), MSG_NOSIGNAL);
        }

        if (!fe.landmarks.empty()) {
            sent = send(crop_sock_fd_, fe.landmarks.data(),
                        fe.landmarks.size() * sizeof(float), MSG_NOSIGNAL);
            if (sent < 0) {
                close_crop_socket();
                return false;
            }
        } else {
            float zero[212] = {};
            send(crop_sock_fd_, zero, sizeof(zero), MSG_NOSIGNAL);
        }
        return true;
    }
    return false;
}

bool RedisBridge::start()
{
    pub_ctx_ = redisConnect(redis_url_.c_str(), 6379);
    if (!pub_ctx_ || pub_ctx_->err) {
        if (pub_ctx_) {
            g_printerr("[RedisBridge] Pub connection error: %s\n", pub_ctx_->errstr);
            redisFree(pub_ctx_);
            pub_ctx_ = nullptr;
        }
        return false;
    }

    sub_ctx_ = redisConnect(redis_url_.c_str(), 6379);
    if (!sub_ctx_ || sub_ctx_->err) {
        if (sub_ctx_) {
            g_printerr("[RedisBridge] Sub connection error: %s\n", sub_ctx_->errstr);
            redisFree(sub_ctx_);
            sub_ctx_ = nullptr;
        }
        redisFree(pub_ctx_);
        pub_ctx_ = nullptr;
        return false;
    }

    g_print("[RedisBridge] Connected to Redis at %s\n", redis_url_.c_str());

    enc_ctx_ = nvds_obj_enc_create_context(0);
    if (!enc_ctx_) {
        g_printerr("[RedisBridge] Failed to create encoder context\n");
    }

    connect_crop_socket();

    running_ = TRUE;
    GThread* th = g_thread_new("redis-subscriber", subscriber_thread_func, this);
    if (!th) {
        g_printerr("[RedisBridge] Failed to create subscriber thread\n");
        running_ = FALSE;
        redisFree(sub_ctx_);
        sub_ctx_ = nullptr;
        redisFree(pub_ctx_);
        pub_ctx_ = nullptr;
        return false;
    }
    subscriber_thread_ = th;
    return true;
}

void RedisBridge::stop()
{
    g_mutex_lock(&lock_);
    running_ = FALSE;
    g_mutex_unlock(&lock_);

    if (subscriber_thread_) {
        g_thread_join(subscriber_thread_);
        subscriber_thread_ = nullptr;
    }

    close_crop_socket();

    if (enc_ctx_) {
        nvds_obj_enc_destroy_context(enc_ctx_);
        enc_ctx_ = nullptr;
    }

    if (sub_ctx_) {
        redisFree(sub_ctx_);
        sub_ctx_ = nullptr;
    }
    if (pub_ctx_) {
        redisFree(pub_ctx_);
        pub_ctx_ = nullptr;
    }
    g_print("[RedisBridge] Stopped\n");
}

void* RedisBridge::subscriber_thread_func(void* arg)
{
    RedisBridge* bridge = static_cast<RedisBridge*>(arg);
    bridge->subscriber_thread();
    return nullptr;
}

void RedisBridge::subscriber_thread()
{
    redisReply* reply = (redisReply*)redisCommand(
        sub_ctx_, "SUBSCRIBE deepstream:commands");
    if (!reply || reply->type == REDIS_REPLY_ERROR) {
        if (reply) {
            g_printerr("[RedisBridge] Subscribe error: %s\n", reply->str);
            freeReplyObject(reply);
        }
        return;
    }
    freeReplyObject(reply);
    g_print("[RedisBridge] Subscribed to deepstream:commands\n");

    while (running_) {
        redisReply* msg = nullptr;
        int ret = redisGetReply(sub_ctx_, (void**)&msg);
        if (ret != REDIS_OK || !msg) {
            g_usleep(100000);
            continue;
        }

        if (msg->type == REDIS_REPLY_ARRAY && msg->elements >= 3) {
            const char* channel = msg->element[1]->str;
            const char* payload = msg->element[2]->str;
            g_print("[RedisBridge] Received on %s\n", channel);
            handle_command(payload);
        }
        freeReplyObject(msg);
    }
}

static size_t curl_write_cb(void* contents, size_t size, size_t nmemb, void* userp)
{
    return size * nmemb;
}

static size_t curl_capture_cb(void* contents, size_t size, size_t nmemb, void* userp)
{
    size_t realsize = size * nmemb;
    std::string* str = static_cast<std::string*>(userp);
    str->append(static_cast<char*>(contents), realsize);
    return realsize;
}

static void post_rest_endpoint(const char* url, const char* json_body)
{
    CURL* curl = curl_easy_init();
    if (!curl) {
        g_printerr("[RedisBridge] curl_easy_init failed\n");
        return;
    }

    struct curl_slist* headers = nullptr;
    headers = curl_slist_append(headers, "Content-Type: application/json");

    curl_easy_setopt(curl, CURLOPT_URL, url);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, json_body);
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, curl_write_cb);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 5L);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);

    CURLcode res = curl_easy_perform(curl);

    if (res != CURLE_OK) {
        g_printerr("[RedisBridge] REST POST failed: %s\n", curl_easy_strerror(res));
    } else {
        long http_code = 0;
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);
        g_print("[RedisBridge] REST POST %s -> HTTP %ld\n", url, http_code);
    }

    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);
}

void RedisBridge::handle_command(const std::string& json_str)
{
    Json::Value root;
    Json::CharReaderBuilder builder;
    std::string errors;
    std::istringstream stream(json_str);

    if (!Json::parseFromStream(builder, stream, &root, &errors)) {
        g_printerr("[RedisBridge] JSON parse error: %s\n", errors.c_str());
        return;
    }

    std::string action = root.get("action", "").asString();
    int device_id = root.get("device_id", 0).asInt();
    std::string rtsp_uri = root.get("rtsp_uri", "").asString();
    std::string camera_name = root.get("camera_name", "").asString();
    std::string camera_id = root.get("camera_id", "").asString();

    g_print("[RedisBridge] action=%s device_id=%d\n", action.c_str(), device_id);

    if (action == "start_preview") {
        if (rtsp_uri.empty()) {
            g_printerr("[RedisBridge] start_preview missing rtsp_uri\n");
            return;
        }
        std::string sid = camera_id.empty() ? std::to_string(device_id) : camera_id;
        std::string name = camera_name.empty() ? sid : camera_name;

        char url[256];
        snprintf(url, sizeof(url), "http://127.0.0.1:%d/api/v1/stream/add", rest_port_);

        std::ostringstream body;
        body << "{\"key\":\"redis-add-" << device_id << "\","
             << "\"value\":{"
             << "\"camera_id\":\"" << sid << "\","
             << "\"camera_name\":\"" << name << "\","
             << "\"camera_url\":\"" << rtsp_uri << "\","
             << "\"change\":\"camera_add\"}}";

        g_print("[RedisBridge] Adding stream: device=%d uri=%s\n",
                device_id, rtsp_uri.c_str());
        post_rest_endpoint(url, body.str().c_str());

        g_usleep(500000);

        char info_url[256];
        snprintf(info_url, sizeof(info_url),
                 "http://127.0.0.1:%d/api/v1/stream/get-stream-info", rest_port_);

        std::string info_response;
        CURL* curl_i = curl_easy_init();
        if (curl_i) {
            curl_easy_setopt(curl_i, CURLOPT_URL, info_url);
            curl_easy_setopt(curl_i, CURLOPT_WRITEFUNCTION, curl_capture_cb);
            curl_easy_setopt(curl_i, CURLOPT_WRITEDATA, &info_response);
            curl_easy_setopt(curl_i, CURLOPT_TIMEOUT, 5L);
            curl_easy_setopt(curl_i, CURLOPT_NOSIGNAL, 1L);
            CURLcode res_i = curl_easy_perform(curl_i);
            if (res_i == CURLE_OK) {
                Json::Value info_root;
                Json::CharReaderBuilder reader;
                std::string parse_errors;
                std::istringstream info_stream(info_response);
                if (Json::parseFromStream(reader, info_stream, &info_root, &parse_errors)) {
                    const Json::Value& streams =
                        info_root["stream-info"]["stream-info"];
                    for (const auto& s : streams) {
                        if (s.get("camera_id", "").asString() == sid) {
                            int src_id = s.get("source_id", 0).asInt();
                            g_mutex_lock(&lock_);
                            source_to_device_[src_id] = device_id;
                            g_mutex_unlock(&lock_);
                            g_print("[RedisBridge] Mapped source_id=%d -> device_id=%d\n",
                                    src_id, device_id);
                        }
                    }
                }
            }
            curl_easy_cleanup(curl_i);
        }
    }
    else if (action == "stop_preview") {
        std::string sid = camera_id.empty() ? std::to_string(device_id) : camera_id;

        char url[256];
        snprintf(url, sizeof(url), "http://127.0.0.1:%d/api/v1/stream/remove", rest_port_);

        std::ostringstream body;
        body << "{\"key\":\"redis-remove-" << device_id << "\","
             << "\"value\":{"
             << "\"camera_id\":\"" << sid << "\","
             << "\"camera_name\":\"\","
             << "\"camera_url\":\"\","
             << "\"change\":\"camera_remove\"}}";

        g_print("[RedisBridge] Removing stream: device=%d\n", device_id);
        post_rest_endpoint(url, body.str().c_str());
    }
    else if (action == "reload_analytics") {
        char url[256];
        snprintf(url, sizeof(url), "http://127.0.0.1:%d/api/v1/analytics/reload-config",
                 rest_port_);

        g_print("[RedisBridge] Reloading analytics config\n");
        post_rest_endpoint(url, "{}");
    }
}

float RedisBridge::compute_quality_score(const std::vector<float>& landmarks,
                                          float bbox_left, float bbox_top,
                                          float bbox_width, float bbox_height)
{
    if (landmarks.size() < 10) return 0.0f;

    size_t num_points = landmarks.size() / 2;
    int points_inside = 0;

    for (size_t i = 0; i < num_points; ++i) {
        float x = landmarks[i * 2];
        float y = landmarks[i * 2 + 1];

        if (x >= 0.0f && x <= 1.0f && y >= 0.0f && y <= 1.0f) {
            points_inside++;
        }
    }

    return (float)points_inside / (float)num_points;
}

bool RedisBridge::extract_sgie_tensor_data(NvDsObjectMeta* parent_obj, guint unique_id,
                                            std::vector<float>& data)
{
    if (!parent_obj) return false;

    for (NvDsMetaList* l_user = parent_obj->obj_user_meta_list; l_user;
         l_user = l_user->next) {
        NvDsUserMeta* user_meta = (NvDsUserMeta*)l_user->data;
        if (!user_meta) continue;
        if (user_meta->base_meta.meta_type != NVDSINFER_TENSOR_OUTPUT_META)
            continue;

        NvDsInferTensorMeta* tm =
            (NvDsInferTensorMeta*)user_meta->user_meta_data;
        if (!tm || tm->unique_id != unique_id) continue;

        for (guint l = 0; l < tm->num_output_layers; ++l) {
            if (!tm->out_buf_ptrs_host || !tm->out_buf_ptrs_host[l])
                continue;

            NvDsInferLayerInfo* layer = &tm->output_layers_info[l];
            if (!layer) continue;

            guint total = layer->inferDims.numElements;
            if (total == 0 || total > 10000) continue;

            float* buf = (float*)tm->out_buf_ptrs_host[l];
            if (!buf) continue;

            for (guint j = 0; j < total; ++j) {
                data.push_back((float)buf[j]);
            }
        }
        return !data.empty();
    }
    return false;
}

std::string RedisBridge::make_detection_json(int device_id, int source_id,
                                              guint64 frame_num,
                                              const std::vector<DetectionObject>& objects,
                                              const std::vector<FaceEmbedding>& face_embeddings)
{
    std::ostringstream oss;
    oss << "{\"code\":\"DeepStreamDetection\","
        << "\"action\":\"Pulse\","
        << "\"timestamp\":" << (time(nullptr) * 1000LL) << ","
        << "\"data\":{"
        << "\"device_id\":" << device_id << ","
        << "\"source\":" << source_id << ","
        << "\"frame_num\":" << frame_num << ","
        << "\"Object\":[";

    for (size_t i = 0; i < objects.size(); ++i) {
        if (i > 0) oss << ",";
        const DetectionObject& obj = objects[i];
        oss << "{"
            << "\"object_id\":" << obj.object_id << ","
            << "\"class_id\":" << obj.class_id << ","
            << "\"class_label\":\"" << obj.class_label << "\","
            << "\"confidence\":" << obj.confidence << ","
            << "\"bbox\":{\"left\":" << obj.left << ",\"top\":" << obj.top
            << ",\"width\":" << obj.width << ",\"height\":" << obj.height << "},"
            << "\"" << obj.label << "\":" << obj.object_id << ","
            << "\"Rect\":["
            << (int)(obj.left * 1600) << "," << (int)(obj.top * 900) << ","
            << (int)((obj.left + obj.width) * 1600) << ","
            << (int)((obj.top + obj.height) * 900) << "]"
            << "}";
    }

    oss << "]";

    if (!face_embeddings.empty()) {
        oss << ",\"Faces\":[";
        for (size_t i = 0; i < face_embeddings.size(); ++i) {
            if (i > 0) oss << ",";
            const FaceEmbedding& fe = face_embeddings[i];
            oss << "{\"object_id\":" << fe.object_id << ","
                << "\"quality_score\":" << fe.quality_score << ",";

            oss << "\"landmarks\":[";
            for (size_t j = 0; j < fe.landmarks.size(); ++j) {
                if (j > 0) oss << ",";
                oss << fe.landmarks[j];
            }
            oss << "],";

            oss << "\"embedding\":[";
            for (size_t j = 0; j < fe.embedding.size(); ++j) {
                if (j > 0) oss << ",";
                oss << fe.embedding[j];
            }
            oss << "]}";
        }
        oss << "]";
    }

    oss << "}}";
    return oss.str();
}

void RedisBridge::publish_detection_json(const std::string& json_str)
{
    g_mutex_lock(&lock_);
    if (pub_ctx_) {
        redisReply* reply = (redisReply*)redisCommand(
            pub_ctx_, "PUBLISH device:%d:events %s", 0, json_str.c_str());
        if (reply) freeReplyObject(reply);
    }
    g_mutex_unlock(&lock_);
}

GstPadProbeReturn RedisBridge::analytics_pad_probe(GstPad* pad, GstPadProbeInfo* info,
                                                    gpointer user_data)
{
    RedisBridge* bridge = static_cast<RedisBridge*>(user_data);
    GstBuffer* buf = GST_BUFFER(info->data);

    GstMapInfo inmap = GST_MAP_INFO_INIT;
    NvBufSurface* ip_surf = nullptr;
    if (gst_buffer_map(buf, &inmap, GST_MAP_READ)) {
        ip_surf = (NvBufSurface*)inmap.data;
    }

    NvDsBatchMeta* batch_meta = gst_buffer_get_nvds_batch_meta(buf);
    if (!batch_meta) {
        if (ip_surf) gst_buffer_unmap(buf, &inmap);
        return GST_PAD_PROBE_OK;
    }

    guint64 current_time = g_get_monotonic_time();
    std::map<int, std::vector<DetectionObject>> per_device_objects;
    std::map<int, std::vector<FaceEmbedding>> per_device_faces;
    std::vector<std::pair<NvDsObjectMeta*, NvDsFrameMeta*>> face_obj_metas;

    int face_index = 0;

    for (NvDsMetaList* l_frame = batch_meta->frame_meta_list; l_frame;
         l_frame = l_frame->next) {
        NvDsFrameMeta* frame_meta = (NvDsFrameMeta*)l_frame->data;
        int source_id = frame_meta->source_id;

        int device_id = source_id;
        g_mutex_lock(&bridge->lock_);
        auto it = bridge->source_to_device_.find(source_id);
        if (it != bridge->source_to_device_.end()) device_id = it->second;
        g_mutex_unlock(&bridge->lock_);

        float frame_w = (float)frame_meta->source_frame_width;
        float frame_h = (float)frame_meta->source_frame_height;
        if (frame_w <= 0) frame_w = 1920;
        if (frame_h <= 0) frame_h = 1080;

        for (NvDsMetaList* l_obj = frame_meta->obj_meta_list; l_obj;
             l_obj = l_obj->next) {
            NvDsObjectMeta* obj_meta = (NvDsObjectMeta*)l_obj->data;

            DetectionObject det;
            det.object_id = obj_meta->object_id;
            det.class_id = obj_meta->class_id;

            g_mutex_lock(&bridge->lock_);
            auto lit = bridge->labels_.find(det.class_id);
            det.class_label = (lit != bridge->labels_.end()) ? lit->second
                                                              : std::to_string(det.class_id);
            g_mutex_unlock(&bridge->lock_);

            det.confidence = obj_meta->confidence;

            det.left = obj_meta->detector_bbox_info.org_bbox_coords.left / frame_w;
            det.top = obj_meta->detector_bbox_info.org_bbox_coords.top / frame_h;
            det.width = obj_meta->detector_bbox_info.org_bbox_coords.width / frame_w;
            det.height = obj_meta->detector_bbox_info.org_bbox_coords.height / frame_h;

            if (det.left < 0.0f) det.left = 0.0f;
            if (det.top < 0.0f) det.top = 0.0f;
            if (det.left + det.width > 1.0f) det.width = 1.0f - det.left;
            if (det.top + det.height > 1.0f) det.height = 1.0f - det.top;

            det.label = (det.class_label.find("person") != std::string::npos ||
                        det.class_label.find("face") != std::string::npos)
                           ? "HumamID" : "VehicleID";

            per_device_objects[device_id].push_back(det);

            if (obj_meta->class_id == 2) {
                FaceEmbedding fe;
                fe.object_id = obj_meta->object_id;

                bool has_lm = bridge->extract_sgie_tensor_data(obj_meta, 2, fe.landmarks);
                bool has_emb = bridge->extract_sgie_tensor_data(obj_meta, 3, fe.embedding);

                if (has_lm && !fe.landmarks.empty()) {
                    fe.quality_score = bridge->compute_quality_score(
                        fe.landmarks, det.left, det.top, det.width, det.height);
                } else {
                    fe.quality_score = 0.0f;
                }

                if (has_emb && !fe.embedding.empty()) {
                    per_device_faces[device_id].push_back(fe);
                }

                if (ip_surf && bridge->enc_ctx_ && bridge->crop_sock_connected_) {
                    NvDsObjEncUsrArgs objData = {};
                    objData.saveImg = TRUE;
                    objData.attachUsrMeta = TRUE;
                    objData.quality = 80;
                    objData.objNum = ++face_index;
                    nvds_obj_enc_process(bridge->enc_ctx_, &objData,
                                         ip_surf, obj_meta, frame_meta);
                    face_obj_metas.push_back({obj_meta, frame_meta});
                }
            }
        }
    }

    if (bridge->enc_ctx_ && ip_surf && !face_obj_metas.empty()) {
        nvds_obj_enc_finish(bridge->enc_ctx_);

        size_t idx = 0;
        for (auto& pair : face_obj_metas) {
            NvDsObjectMeta* obj_meta = pair.first;
            int device_id = 1;
            {
                g_mutex_lock(&bridge->lock_);
                auto it = bridge->source_to_device_.find(
                    ((NvDsFrameMeta*)pair.second)->source_id);
                if (it != bridge->source_to_device_.end())
                    device_id = it->second;
                else
                    bridge->source_to_device_[((NvDsFrameMeta*)pair.second)->source_id] = 1;
                g_mutex_unlock(&bridge->lock_);
            }

            const FaceEmbedding* fe = nullptr;
            auto f_map_it = per_device_faces.find(device_id == 0
                ? ((NvDsFrameMeta*)pair.second)->source_id : device_id);
            if (f_map_it != per_device_faces.end() && idx < f_map_it->second.size()) {
                fe = &f_map_it->second[idx];
            }
            FaceEmbedding dummy;
            dummy.object_id = obj_meta->object_id;
            dummy.quality_score = 0.0f;
            if (!fe) fe = &dummy;

            bridge->send_face_crop(obj_meta, pair.second, device_id, *fe);
            idx++;
        }
    }

    if (ip_surf) gst_buffer_unmap(buf, &inmap);

    if (current_time - bridge->last_flush_time_ >= FLUSH_INTERVAL_US) {
        for (auto& kv : per_device_objects) {
            int device_id = kv.first;
            std::vector<DetectionObject>& objects = kv.second;
            if (objects.empty()) continue;

            std::vector<FaceEmbedding> faces;
            auto f_it = per_device_faces.find(device_id);
            if (f_it != per_device_faces.end()) {
                faces = f_it->second;
            }

            std::string json = bridge->make_detection_json(
                device_id, 0, 0, objects, faces);

            g_mutex_lock(&bridge->lock_);
            if (bridge->pub_ctx_) {
                char channel[128];
                snprintf(channel, sizeof(channel), "device:%d:events", device_id);
                redisReply* reply = (redisReply*)redisCommand(
                    bridge->pub_ctx_, "PUBLISH %s %s", channel, json.c_str());
                if (reply) freeReplyObject(reply);
            }
            g_mutex_unlock(&bridge->lock_);

            g_print("[AnalyticsProbe] device=%d objects=%zu faces=%zu\n",
                    device_id, objects.size(), faces.size());
        }
        bridge->last_flush_time_ = current_time;
    }

    return GST_PAD_PROBE_OK;
}
