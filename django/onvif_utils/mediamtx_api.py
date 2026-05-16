import os
from urllib.parse import urlparse, urlunparse, quote

import requests


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
        """Return Basic auth headers using admin/mediamtx_admin_pass."""
        if self.api_key:
            import base64

            creds = base64.b64encode(b"admin:mediamtx_admin_pass").decode()
            return {"Authorization": f"Basic {creds}"}
        return {}

    def _post(self, path, **kwargs):
        """POST to path on MediaMTX API, return parsed JSON."""
        resp = requests.post(
            f"{self.base_url}{path}",
            headers=self._headers(),
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()

    def _get(self, path, **kwargs):
        """GET from path on MediaMTX API, return parsed JSON."""
        resp = requests.get(
            f"{self.base_url}{path}",
            headers=self._headers(),
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path, **kwargs):
        """DELETE path on MediaMTX API, return parsed JSON."""
        resp = requests.delete(
            f"{self.base_url}{path}",
            headers=self._headers(),
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()

    def add_path(
        self,
        name,
        source=None,
        run_on_init=None,
        run_on_init_restart=False,
        run_on_ready=None,
        run_on_ready_restart=False,
    ):
        """Register a new stream path in MediaMTX.

        Args:
            name: Path name (e.g. "cam_1_profile0").
            source: RTSP source URL for pull mode, or "publisher" for push.
            run_on_init: Command to run when path is created.
            run_on_init_restart: Restart command if it exits.
            run_on_ready: Command when source is ready (used for ffmpeg transcoding).
            run_on_ready_restart: Restart command if it exits.
        """
        body = {}
        if source is not None:
            body["source"] = source
        if run_on_init is not None:
            body["runOnInit"] = run_on_init
            body["runOnInitRestart"] = run_on_init_restart
        if run_on_ready is not None:
            body["runOnReady"] = run_on_ready
            body["runOnReadyRestart"] = run_on_ready_restart
        return self._post(f"/v3/config/paths/add/{name}", json=body)

    def delete_path(self, name):
        """Remove a stream path by name."""
        return self._delete(f"/v3/config/paths/delete/{name}")

    def list_paths(self):
        """Return list of all configured stream paths."""
        data = self._get("/v3/config/paths/list")
        return data.get("items", [])

    def camera_paths(self, device_id):
        """Return only the stream paths belonging to a device."""
        prefix = f"cam_{device_id}_"
        return [p for p in self.list_paths() if p.get("name", "").startswith(prefix)]

    def delete_camera_paths(self, device_id):
        """Remove all stream paths for a device."""
        paths = self.camera_paths(device_id)
        for p in paths:
            try:
                self.delete_path(p["name"])
            except requests.RequestException as e:
                print(f"Error deleting path {p['name']}: {e}")

    def _encode_rtsp_url(self, uri):
        """Percent-encode username/password in an RTSP URL.

        MediaMTX's internal RTSP client rejects special chars (e.g. ``+``)
        in credentials, so we encode them via ``urllib.parse.quote``.
        """
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

    def ensure_camera_streams(self, device_id, profiles, stream_uris):
        """Create raw + transcoded H264 stream paths for a device.

        For each profile:
          1. Create ``cam_{id}_{token}_hw`` as a ``source="publisher"`` path
             (receives ffmpeg output).
          2. Create ``cam_{id}_{token}`` pulling from the camera's RTSP URI,
             with ``runOnReady`` that runs ffmpeg to transcode to the ``_hw`` path.

        Skips paths that already exist in MediaMTX.
        """
        existing = {p["name"] for p in self.list_paths()}

        for profile_token, stream_uri in zip(profiles, stream_uris):
            raw_name = f"cam_{device_id}_{profile_token}"
            hw_name = f"{raw_name}_hw"

            ffmpeg_cmd = (
                f'ffmpeg -rtsp_transport tcp -i "rtsp://127.0.0.1:8554/{raw_name}" '
                f"-c:v libx264 -preset ultrafast -tune zerolatency -c:a copy "
                f'-f rtsp "rtsp://127.0.0.1:8554/{hw_name}"'
            )

            if hw_name not in existing:
                try:
                    self.add_path(hw_name, source="publisher")
                    existing.add(hw_name)
                except requests.RequestException as e:
                    print(f"Error adding path {hw_name}: {e}")

            if raw_name not in existing:
                try:
                    encoded_source = self._encode_rtsp_url(stream_uri)
                    self.add_path(
                        raw_name,
                        source=encoded_source,
                        run_on_ready=ffmpeg_cmd,
                        run_on_ready_restart=True,
                    )
                    existing.add(raw_name)
                except requests.RequestException as e:
                    print(f"Error adding path {raw_name}: {e}")
