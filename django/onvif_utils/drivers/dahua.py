import re
from datetime import datetime, timezone

import requests
from requests.auth import HTTPDigestAuth

from onvif_utils.drivers.base import CameraDriver, DriverError


SEGMENT_RE = re.compile(r"^(\w+)(?:\[(\d+)\])?$")


class DahuaDriver(CameraDriver):
    def __init__(self, device):
        super().__init__(device)
        self._base_url = f"http://{device.host}:{device.port}"
        self._auth = HTTPDigestAuth(device.username, device.password)
        self._session = requests.Session()

    def detect(self):
        return "dahua"

    # ------------------------------------------------------------------
    # Motion detection config
    # ------------------------------------------------------------------

    def get_motion_config(self):
        raw = self._cgi_get("getConfig", {"name": "MotionDetect"})
        return _parse_dahua_table(raw)

    def set_motion_config(self, config):
        body = _serialize_dahua_table(config, prefix="MotionDetect")
        if not body:
            raise DriverError("empty config, nothing to set")
        resp = self._cgi_post("setConfig", body)
        if resp.strip() != "OK":
            raise DriverError(f"Dahua setConfig failed: {resp.strip()}")

    # ------------------------------------------------------------------
    # Motion status polling
    # ------------------------------------------------------------------

    def poll_motion(self):
        raw = self._cgi_event_get("getCurrentEvents")
        motion_active = "VideoMotion" in raw
        return {
            "motion": motion_active,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": {},
        }

    def get_capabilities(self):
        return {
            "motion_detection": True,
            "windows": 4,
            "region_bitmask": True,
            "time_sections": True,
            "event_handlers": ["record", "alarm", "snapshot", "email"],
            "brand": "dahua",
        }

    # ------------------------------------------------------------------
    # CGI helpers
    # ------------------------------------------------------------------

    def _cgi_event_get(self, action, params=None):
        url = f"{self._base_url}/cgi-bin/eventManager.cgi"
        merged = {"action": action}
        if params:
            merged.update(params)
        try:
            resp = self._session.get(url, params=merged, auth=self._auth, timeout=10)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            raise DriverError(f"Dahua event CGI failed: {e}") from e

    def _cgi_get(self, action, params):
        url = f"{self._base_url}/cgi-bin/configManager.cgi"
        merged = {"action": action}
        merged.update(params)
        try:
            resp = self._session.get(url, params=merged, auth=self._auth, timeout=10)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            raise DriverError(f"Dahua CGI GET failed: {e}") from e

    def _cgi_post(self, action, body_lines):
        url = f"{self._base_url}/cgi-bin/configManager.cgi"
        params = {"action": action}
        data = "\r\n".join(body_lines)
        try:
            resp = self._session.post(
                url,
                params=params,
                data=data,
                auth=self._auth,
                timeout=10,
                headers={"Content-Type": "text/plain"},
            )
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            raise DriverError(f"Dahua CGI POST failed: {e}") from e


def _parse_dahua_table(raw):
    """Parse Dahua CGI key=value response into a nested dict.

    Input lines:
        table.MotionDetect[0].Enable=true
        table.MotionDetect[0].Window[0].Sensitive=4

    Output:
        {"MotionDetect": [{"Enable": "true", "Window": [{"Sensitive": "4"}]}]}
    """
    result = {}
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith("table."):
            continue
        eq = line.find("=")
        if eq == -1:
            continue
        key = line[6:eq]
        value = line[eq + 1 :].strip()

        segments = key.split(".")
        obj = result
        for i, segment in enumerate(segments):
            m = SEGMENT_RE.match(segment)
            if not m:
                break
            name = m.group(1)
            idx = int(m.group(2)) if m.group(2) else None
            if i == len(segments) - 1:
                if idx is not None:
                    _ensure_list(obj, name, idx)
                    obj[name][idx] = value
                else:
                    obj[name] = value
            else:
                if idx is not None:
                    _ensure_list(obj, name, idx)
                    obj = obj[name][idx]
                else:
                    if name not in obj:
                        obj[name] = {}
                    obj = obj[name]
    return result


def _serialize_dahua_table(config, prefix):
    """Flatten a nested config dict back to Dahua key=value lines.

    Inverse of _parse_dahua_table.  ``config`` is expected to be a list
    of one element (the [0] entry) as returned by get_motion_config().

    Example input:
        {"MotionDetect": [{"Enable": "true", "Window": [{"Sensitive": "4"}]}]}

    Output:
        ["table.MotionDetect[0].Enable=true",
         "table.MotionDetect[0].Window[0].Sensitive=4"]
    """
    if not isinstance(config, dict) or prefix not in config:
        return []
    lines = []
    _flatten(lines, prefix, config[prefix])
    return lines


def _flatten(lines, path, value):
    if isinstance(value, list):
        for idx, item in enumerate(value):
            _flatten(lines, f"{path}[{idx}]", item)
    elif isinstance(value, dict):
        for key, sub in value.items():
            _flatten(lines, f"{path}.{key}", sub)
    else:
        lines.append(f"table.{path}={value}")


def _ensure_list(container, key, idx):
    if key not in container:
        container[key] = []
    arr = container[key]
    while len(arr) <= idx:
        arr.append({})
    if arr[idx] is None:
        arr[idx] = {}
