#ifndef SNAPSHOT_SENDER_H
#define SNAPSHOT_SENDER_H

#include <gst/gst.h>
#include <gstnvdsmeta.h>
#include <nvbufsurface.h>
#include <string>
#include <cstdint>

extern "C" {
#include "nvds_obj_encode.h"
}

#define SNAPSHOT_TYPE_LEN 16

#pragma pack(push, 1)
struct SnapshotPacket {
    uint32_t device_id;
    uint32_t source_id;
    char     snapshot_type[SNAPSHOT_TYPE_LEN];
    float    bbox_left;
    float    bbox_top;
    float    bbox_width;
    float    bbox_height;
    uint64_t timestamp_ms;
    uint32_t jpeg_size;
};
#pragma pack(pop)

class SnapshotSender {
public:
    SnapshotSender(const std::string& host, int port, const std::string& snap_type);
    ~SnapshotSender();

    bool start();
    void stop();

    bool send_full_frame(NvBufSurface* surf, NvDsFrameMeta* frame_meta,
                         uint32_t device_id, uint32_t source_id);

    bool send_obj_crop(NvBufSurface* surf, NvDsObjectMeta* obj_meta,
                       NvDsFrameMeta* frame_meta, uint32_t device_id, uint32_t source_id);

    bool is_connected() const { return sock_connected_; }

    void set_quality(int q) { encode_quality_ = q; }

private:
    bool connect_tcp();
    void close_tcp();
    bool encode_and_send(NvBufSurface* surf, NvDsObjectMeta* obj_meta,
                         NvDsFrameMeta* frame_meta, uint32_t device_id,
                         uint32_t source_id, float bl, float bt, float bw, float bh);

    std::string host_;
    int port_;
    std::string snap_type_;
    char snap_type_buf_[SNAPSHOT_TYPE_LEN];

    int sock_fd_;
    bool sock_connected_;
    NvDsObjEncCtxHandle enc_ctx_;
    int obj_counter_;
    int encode_quality_;
};

#endif
