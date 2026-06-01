#include "snapshot_sender.h"
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <netdb.h>
#include <unistd.h>
#include <cstring>
#include <ctime>

#define END_MARKER "END!"
#define END_MARKER_LEN 4

SnapshotSender::SnapshotSender(const std::string& host, int port,
                               const std::string& snap_type)
    : host_(host), port_(port), snap_type_(snap_type),
      sock_fd_(-1), sock_connected_(false),
      enc_ctx_(nullptr), obj_counter_(0), encode_quality_(80)
{
    memset(snap_type_buf_, 0, SNAPSHOT_TYPE_LEN);
    snap_type_.copy(snap_type_buf_, SNAPSHOT_TYPE_LEN - 1);
}

SnapshotSender::~SnapshotSender()
{
    stop();
}

bool SnapshotSender::start()
{
    enc_ctx_ = nvds_obj_enc_create_context(0);
    if (!enc_ctx_) {
        g_printerr("[SnapshotSender:%s] Failed to create encoder context\n",
                   snap_type_.c_str());
        return false;
    }
    connect_tcp();
    g_print("[SnapshotSender:%s] Started (host=%s port=%d)\n",
            snap_type_.c_str(), host_.c_str(), port_);
    return true;
}

void SnapshotSender::stop()
{
    close_tcp();
    if (enc_ctx_) {
        nvds_obj_enc_destroy_context(enc_ctx_);
        enc_ctx_ = nullptr;
    }
    g_print("[SnapshotSender:%s] Stopped\n", snap_type_.c_str());
}

bool SnapshotSender::connect_tcp()
{
    sock_fd_ = socket(AF_INET, SOCK_STREAM, 0);
    if (sock_fd_ < 0) {
        g_printerr("[SnapshotSender:%s] socket() failed\n", snap_type_.c_str());
        return false;
    }

    int flag = 1;
    setsockopt(sock_fd_, IPPROTO_TCP, TCP_NODELAY, &flag, sizeof(flag));

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port_);
    addr.sin_addr.s_addr = inet_addr(host_.c_str());

    if (addr.sin_addr.s_addr == INADDR_NONE) {
        struct hostent* he = gethostbyname(host_.c_str());
        if (!he) {
            g_printerr("[SnapshotSender:%s] DNS lookup failed for %s\n",
                       snap_type_.c_str(), host_.c_str());
            close(sock_fd_);
            sock_fd_ = -1;
            return false;
        }
        memcpy(&addr.sin_addr, he->h_addr, he->h_length);
    }

    if (connect(sock_fd_, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        g_printerr("[SnapshotSender:%s] TCP connect to %s:%d failed\n",
                   snap_type_.c_str(), host_.c_str(), port_);
        close(sock_fd_);
        sock_fd_ = -1;
        return false;
    }

    sock_connected_ = true;
    g_print("[SnapshotSender:%s] TCP connected to %s:%d\n",
            snap_type_.c_str(), host_.c_str(), port_);
    return true;
}

void SnapshotSender::close_tcp()
{
    sock_connected_ = false;
    if (sock_fd_ >= 0) {
        shutdown(sock_fd_, SHUT_RDWR);
        close(sock_fd_);
        sock_fd_ = -1;
    }
}

bool SnapshotSender::send_full_frame(NvBufSurface* surf, NvDsFrameMeta* frame_meta,
                                      uint32_t device_id, uint32_t source_id)
{
    if (!sock_connected_ && !connect_tcp()) return false;

    float fw = (float)frame_meta->source_frame_width;
    float fh = (float)frame_meta->source_frame_height;
    if (fw <= 0) fw = 1920;
    if (fh <= 0) fh = 1080;

    NvDsObjEncUsrArgs objData = {};
    objData.saveImg = FALSE;
    objData.attachUsrMeta = TRUE;
    objData.quality = encode_quality_;
    objData.objNum = ++obj_counter_;

    objData.scaleImg = TRUE;
    objData.scaledWidth = 960;
    objData.scaledHeight = 540;

    float saved_l = 0, saved_t = 0, saved_w = 0, saved_h = 0;
    bool had_bbox = false;

    NvDsObjectMeta* obj_meta = nullptr;
    for (NvDsMetaList* lo = frame_meta->obj_meta_list; lo; lo = lo->next) {
        obj_meta = (NvDsObjectMeta*)lo->data;
        if (obj_meta) break;
    }

    if (obj_meta) {
        saved_l = obj_meta->detector_bbox_info.org_bbox_coords.left;
        saved_t = obj_meta->detector_bbox_info.org_bbox_coords.top;
        saved_w = obj_meta->detector_bbox_info.org_bbox_coords.width;
        saved_h = obj_meta->detector_bbox_info.org_bbox_coords.height;
        had_bbox = true;

        obj_meta->detector_bbox_info.org_bbox_coords.left = 0;
        obj_meta->detector_bbox_info.org_bbox_coords.top = 0;
        obj_meta->detector_bbox_info.org_bbox_coords.width = fw;
        obj_meta->detector_bbox_info.org_bbox_coords.height = fh;
    }

    nvds_obj_enc_process(enc_ctx_, &objData, surf, obj_meta, frame_meta);
    nvds_obj_enc_finish(enc_ctx_);

    if (had_bbox) {
        obj_meta->detector_bbox_info.org_bbox_coords.left = saved_l;
        obj_meta->detector_bbox_info.org_bbox_coords.top = saved_t;
        obj_meta->detector_bbox_info.org_bbox_coords.width = saved_w;
        obj_meta->detector_bbox_info.org_bbox_coords.height = saved_h;
    }

    bool sent = false;

    for (NvDsMetaList* l_user = obj_meta->obj_user_meta_list; l_user;
         l_user = l_user->next) {
        NvDsUserMeta* user_meta = (NvDsUserMeta*)l_user->data;
        if (!user_meta) continue;
        if (user_meta->base_meta.meta_type != NVDS_CROP_IMAGE_META)
            continue;

        NvDsObjEncOutParams* enc = (NvDsObjEncOutParams*)user_meta->user_meta_data;
        if (!enc || !enc->outBuffer || enc->outLen == 0) continue;

        SnapshotPacket pkt;
        memset(&pkt, 0, sizeof(pkt));
        pkt.device_id = device_id;
        pkt.source_id = source_id;
        memcpy(pkt.snapshot_type, snap_type_buf_, SNAPSHOT_TYPE_LEN);
        pkt.bbox_left = 0;
        pkt.bbox_top = 0;
        pkt.bbox_width = 0;
        pkt.bbox_height = 0;
        pkt.timestamp_ms = (uint64_t)(time(nullptr) * 1000LL);
        pkt.jpeg_size = (uint32_t)enc->outLen;

        ssize_t s;
        s = send(sock_fd_, enc->outBuffer, enc->outLen, MSG_NOSIGNAL);
        if (s < 0) { close_tcp(); break; }

        s = send(sock_fd_, END_MARKER, END_MARKER_LEN, MSG_NOSIGNAL);
        if (s < 0) { close_tcp(); break; }

        s = send(sock_fd_, &pkt, sizeof(pkt), MSG_NOSIGNAL);
        if (s < 0) { close_tcp(); break; }

        sent = true;
        g_print("[SnapshotSender:%s] Sent %u bytes for device=%d\n",
                snap_type_.c_str(), pkt.jpeg_size, device_id);
        break;
    }

    return sent;
}

bool SnapshotSender::send_obj_crop(NvBufSurface* surf, NvDsObjectMeta* obj_meta,
                                    NvDsFrameMeta* frame_meta, uint32_t device_id,
                                    uint32_t source_id)
{
    if (!sock_connected_ && !connect_tcp()) return false;

    float fw = (float)frame_meta->source_frame_width;
    float fh = (float)frame_meta->source_frame_height;
    if (fw <= 0) fw = 1920;
    if (fh <= 0) fh = 1080;

    NvDsObjEncUsrArgs objData = {};
    objData.saveImg = FALSE;
    objData.attachUsrMeta = TRUE;
    objData.quality = encode_quality_;
    objData.objNum = ++obj_counter_;

    nvds_obj_enc_process(enc_ctx_, &objData, surf, obj_meta, frame_meta);
    nvds_obj_enc_finish(enc_ctx_);

    bool sent = false;

    for (NvDsMetaList* l_user = obj_meta->obj_user_meta_list; l_user;
         l_user = l_user->next) {
        NvDsUserMeta* user_meta = (NvDsUserMeta*)l_user->data;
        if (!user_meta) continue;
        if (user_meta->base_meta.meta_type != NVDS_CROP_IMAGE_META)
            continue;

        NvDsObjEncOutParams* enc = (NvDsObjEncOutParams*)user_meta->user_meta_data;
        if (!enc || !enc->outBuffer || enc->outLen == 0) continue;

        SnapshotPacket pkt;
        memset(&pkt, 0, sizeof(pkt));
        pkt.device_id = device_id;
        pkt.source_id = source_id;
        memcpy(pkt.snapshot_type, snap_type_buf_, SNAPSHOT_TYPE_LEN);
        pkt.bbox_left = obj_meta->detector_bbox_info.org_bbox_coords.left / fw;
        pkt.bbox_top = obj_meta->detector_bbox_info.org_bbox_coords.top / fh;
        pkt.bbox_width = obj_meta->detector_bbox_info.org_bbox_coords.width / fw;
        pkt.bbox_height = obj_meta->detector_bbox_info.org_bbox_coords.height / fh;
        pkt.timestamp_ms = (uint64_t)(time(nullptr) * 1000LL);
        pkt.jpeg_size = (uint32_t)enc->outLen;

        ssize_t s;
        s = send(sock_fd_, enc->outBuffer, enc->outLen, MSG_NOSIGNAL);
        if (s < 0) { close_tcp(); break; }

        s = send(sock_fd_, END_MARKER, END_MARKER_LEN, MSG_NOSIGNAL);
        if (s < 0) { close_tcp(); break; }

        s = send(sock_fd_, &pkt, sizeof(pkt), MSG_NOSIGNAL);
        if (s < 0) { close_tcp(); break; }

        sent = true;
        break;
    }

    return sent;
}
