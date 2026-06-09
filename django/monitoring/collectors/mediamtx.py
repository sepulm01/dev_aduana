import base64
import logging

import requests

logger = logging.getLogger(__name__)

MEDIAMTX_API = "http://mediamtx:9997"
MEDIAMTX_AUTH = base64.b64encode(b"admin:mediamtx_admin_pass").decode()


def collect_mediamtx_metrics():
    headers = {"Authorization": f"Basic {MEDIAMTX_AUTH}"}
    data = {
        "paths": _list_paths(headers),
        "rtsp_sessions": _list_sessions(headers, "rtspsessions"),
        "webrtc_sessions": _list_sessions(headers, "webrtcsessions"),
    }
    return data


def _list_paths(headers):
    try:
        resp = requests.get(
            f"{MEDIAMTX_API}/v3/paths/list", headers=headers, timeout=10
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except Exception as e:
        logger.warning("MediaMTX paths error: %s", e)
        return []

    paths = []
    for p in items:
        paths.append(
            {
                "name": p.get("name", ""),
                "ready": p.get("ready", False),
                "available": p.get("available", False),
                "online": p.get("online", False),
                "tracks": p.get("tracks", []),
                "readers_count": len(p.get("readers", [])),
                "inbound_mb": round(p.get("bytesReceived", 0) / 1024 / 1024, 1),
                "outbound_mb": round(p.get("bytesSent", 0) / 1024 / 1024, 1),
                "inbound_frames_in_error": p.get("inboundFramesInError", 0),
            }
        )
    return paths


def _list_sessions(headers, endpoint):
    try:
        resp = requests.get(
            f"{MEDIAMTX_API}/v3/{endpoint}/list", headers=headers, timeout=10
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except Exception as e:
        logger.warning("MediaMTX %s error: %s", endpoint, e)
        return []

    sessions = []
    for s in items:
        sessions.append(
            {
                "id": s.get("id", "")[:12],
                "state": s.get("state", ""),
                "path": s.get("path", ""),
                "remote_addr": s.get("remoteAddr", ""),
                "rtp_packets_sent": s.get("rtpPacketsSent", 0),
                "rtp_packets_lost": s.get("rtpPacketsLost", 0),
                "rtp_packets_jitter": s.get("rtpPacketsJitter", 0),
                "outbound_frames_discarded": s.get("outboundFramesDiscarded", 0),
            }
        )
    return sessions
