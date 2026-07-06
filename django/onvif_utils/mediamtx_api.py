import base64
import os
from urllib.parse import urlparse, urlunparse, quote

import requests
import yaml


CONFIG_PATH = os.environ.get("MEDIAMTX_CONFIG_PATH", "/app/mediamtx.yml")


class MediaMTXAPI:
    """Client for the MediaMTX REST API.

    Manages camera stream paths (raw RTSP + transcoded H264) via
    MediaMTX's /v3/config/paths/ endpoints on port 9997.
    """

    def __init__(self, base_url=None, api_key=None):
        self.base_url = base_url or os.environ.get(
            "MEDIAMTX_API_URL", "http://127.0.0.1:9997"
        )
        self.api_key = api_key or os.environ.get("MEDIAMTX_API_KEY", "")

    def _headers(self):
        creds = base64.b64encode(b"admin:mediamtx_admin_pass").decode()
        return {"Authorization": f"Basic {creds}"}

    def _post(self, path, **kwargs):
        resp = requests.post(
            f"{self.base_url}{path}",
            headers=self._headers(),
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()

    def _get(self, path, **kwargs):
        resp = requests.get(
            f"{self.base_url}{path}",
            headers=self._headers(),
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path, **kwargs):
        resp = requests.delete(
            f"{self.base_url}{path}",
            headers=self._headers(),
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()

    def reload_config(self):
        try:
            import subprocess
            subprocess.run(
                ["docker", "kill", "-s", "USR1", "aduana-mediamtx-1"],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass

    def list_paths(self):
        data = self._get("/v3/config/paths/list")
        return data.get("items", [])

    def camera_paths(self, device_id):
        prefix = f"cam_{device_id}_"
        return [p for p in self.list_paths() if p.get("name", "").startswith(prefix)]

    def ensure_camera_streams(self, device_id, profiles, stream_uris):
        try:
            self._write_config(device_id, profiles, stream_uris)
            self.reload_config()
        except Exception as e:
            print(f"Error writing config for device {device_id}: {e}")

    def _write_config(self, device_id, profiles, stream_uris):
        try:
            with open(CONFIG_PATH, "r") as f:
                config = yaml.safe_load(f) or {}
        except FileNotFoundError:
            config = {}

        if "paths" not in config:
            config["paths"] = {}

        prefix = f"cam_{device_id}_"

        config["paths"] = {
            name: cfg
            for name, cfg in config["paths"].items()
            if not name.startswith(prefix)
        }

        for profile_token, stream_uri in zip(profiles, stream_uris):
            hw_name = f"cam_{device_id}_{profile_token}_hw"
            encoded_source = self._encode_rtsp_url(stream_uri)
            ffmpeg_cmd = (
                f"ffmpeg -rtsp_transport tcp -i {encoded_source} "
                f"-c:v libx264 -preset ultrafast -tune zerolatency -c:a copy "
                f'-f rtsp "rtsp://127.0.0.1:8554/{hw_name}"'
            )
            config["paths"][hw_name] = {
                "source": "publisher",
                "runOnDemand": ffmpeg_cmd,
                "runOnDemandRestart": True,
            }

        with open(CONFIG_PATH, "w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

    def _encode_rtsp_url(self, uri):
        parsed = urlparse(uri)
        if parsed.username:
            encoded_username = quote(parsed.username, safe="")
            encoded_password = (
                quote(parsed.password, safe="") if parsed.password else ""
            )
            netloc = f"{encoded_username}:{encoded_password}@{parsed.hostname}"
            if parsed.port:
                netloc += f":{parsed.port}"
            return urlunparse(
                (
                    parsed.scheme,
                    netloc,
                    parsed.path,
                    parsed.params,
                    parsed.query,
                    parsed.fragment,
                )
            )
        return uri
