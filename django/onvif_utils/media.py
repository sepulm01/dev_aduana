from urllib.parse import urlparse, urlunparse


class MediaService:
    """Wraps the ONVIF Media SOAP service for profile and stream URI queries."""

    def __init__(self, client):
        self.client = client

    def get_profiles(self):
        """Return a list of profile dicts with video/audio encoder and PTZ info."""
        profiles = self.client.media.GetProfiles()
        result = []
        for p in profiles:
            profile = {
                "token": getattr(p, "token", None) or getattr(p, "_token", ""),
                "name": getattr(p, "Name", ""),
                "video_encoder": None,
                "audio_encoder": None,
                "ptz": None,
            }

            if hasattr(p, "VideoEncoderConfiguration") and p.VideoEncoderConfiguration:
                enc = p.VideoEncoderConfiguration
                profile["video_encoder"] = {
                    "token": getattr(enc, "token", None) or getattr(enc, "_token", ""),
                    "encoding": getattr(enc, "Encoding", ""),
                    "resolution": {
                        "width": enc.Resolution.Width,
                        "height": enc.Resolution.Height,
                    }
                    if hasattr(enc, "Resolution") and enc.Resolution
                    else {},
                    "quality": getattr(enc, "Quality", None),
                }

            if hasattr(p, "AudioEncoderConfiguration") and p.AudioEncoderConfiguration:
                profile["audio_encoder"] = {
                    "token": getattr(p.AudioEncoderConfiguration, "token", "")
                }

            if hasattr(p, "PTZConfiguration") and p.PTZConfiguration:
                profile["ptz"] = {"token": getattr(p.PTZConfiguration, "token", "")}

            result.append(profile)
        return result

    def get_stream_uri(
        self, profile_token, protocol="RTSP", username=None, password=None
    ):
        """Get the RTSP stream URI for a profile, optionally injecting credentials."""
        try:
            uri = self.client.media.GetStreamUri(
                {
                    "StreamSetup": {
                        "Stream": "RTP-Unicast",
                        "Transport": {"Protocol": protocol},
                    },
                    "ProfileToken": profile_token,
                }
            )
            raw_uri = uri.Uri if hasattr(uri, "Uri") else None
            if not raw_uri:
                return None
            if username and password:
                parsed = urlparse(raw_uri)
                netloc = f"{username}:{password}@{parsed.hostname}"
                if parsed.port:
                    netloc += f":{parsed.port}"
                raw_uri = urlunparse(
                    (
                        parsed.scheme,
                        netloc,
                        parsed.path,
                        parsed.params,
                        parsed.query,
                        parsed.fragment,
                    )
                )
            return raw_uri
        except Exception:
            return None
