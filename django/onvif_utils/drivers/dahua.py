import json
import logging
import re
import threading
from datetime import datetime, timezone

import requests
from requests.auth import HTTPDigestAuth

from onvif_utils.client import OnvifClient
from onvif_utils.drivers.base import CameraDriver, DriverError

logger = logging.getLogger(__name__)


SEGMENT_RE = re.compile(r"^([A-Za-z_]\w*)((?:\[\d+\])*)$")


class DahuaDriver(CameraDriver):
    """Driver for Dahua cameras using HTTP CGI (configManager.cgi / eventManager.cgi).

    Communicates with the camera's native CGI API via Digest authentication,
    bypassing ONVIF for motion configuration and event polling.
    """

    def __init__(self, device):
        super().__init__(device)
        self._base_url = f"http://{device.host}:{device.port}"
        self._auth = HTTPDigestAuth(device.username, device.password)
        self._session = requests.Session()

    def detect(self):
        """Return driver identifier string ("dahua")."""
        return "dahua"

    # ------------------------------------------------------------------
    # Motion detection config
    # ------------------------------------------------------------------

    def get_motion_config(self):
        """Read motion detection configuration from the camera."""
        raw = self._cgi_get("getConfig", {"name": "MotionDetect"})
        return _parse_dahua_table(raw)

    def set_motion_config(self, config):
        """Write motion detection configuration to the camera.

        Raises DriverError if the camera rejects the config.
        """
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
        """Poll the camera for current VideoMotion event status."""
        raw = self._cgi_event_get("getCurrentEvents")
        motion_active = "VideoMotion" in raw
        return {
            "motion": motion_active,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": {},
        }

    def get_capabilities(self):
        """Return hardcoded driver capabilities dict for Dahua cameras."""
        return {
            "motion_detection": True,
            "windows": 4,
            "region_bitmask": True,
            "time_sections": True,
            "event_handlers": ["record", "alarm", "snapshot", "email"],
            "brand": "dahua",
            "ivs": True,
        }

    def ping(self):
        """Ping the camera using ONVIF GetDeviceInformation."""
        try:
            client = OnvifClient(
                self.device.host,
                self.device.port,
                self.device.username,
                self.device.password,
            )
            client.get_device_info()
            return {"online": True, "last_seen": datetime.now(timezone.utc)}
        except Exception:
            return {"online": False, "last_seen": None}

    # ------------------------------------------------------------------
    # IVS rules
    # ------------------------------------------------------------------

    def get_ivs_rules(self):
        """Read IVS rules from the camera via VideoAnalyseRule config."""
        raw = self._cgi_get("getConfig", {"name": "VideoAnalyseRule"})
        parsed = _parse_dahua_table(raw)
        rules = []
        rule_table = parsed.get("VideoAnalyseRule", [])

        if isinstance(rule_table, list):
            items = enumerate(rule_table)
        elif isinstance(rule_table, dict):
            items = sorted(
                rule_table.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0
            )
        else:
            items = []

        for idx, rule in items:
            if not isinstance(rule, dict):
                continue
            inner = rule
            if len(inner) == 1 and isinstance(list(inner.values())[0], list):
                inner = list(inner.values())[0]
                if inner and isinstance(inner[0], dict):
                    inner = inner[0]
            rules.append(
                {
                    "index": idx,
                    "enable": inner.get("Enable", "false").lower() == "true",
                    "name": inner.get("Name", f"Rule {idx}"),
                    "type": inner.get("Type", ""),
                    "direction": inner.get("Direction", "Both"),
                    "detect_line": inner.get("DetectLine", ""),
                    "detect_region": inner.get("DetectRegion", ""),
                    "event_handler": json.loads(inner.get("EventHandler", "{}")),
                }
            )
        return rules

    def set_ivs_rules(self, rules):
        """Write IVS rules to the camera via setConfig.

        Dahua setConfig uses GET with query parameters, NOT POST body:
            configManager.cgi?action=setConfig&VideoAnalyseRule[0][N].Field=value

        IMPORTANT: Dahua CGI only supports max 2 fields per request.
        Fields like DetectLine and DetectRegion may not be writable on all cameras.

        Note: SmartPlan must be enabled on the camera via its web interface
        (Settings > Event > Smart Plan > IVS) before IVS rules can function.
        """
        for idx, rule in enumerate(rules):
            prefix = f"VideoAnalyseRule[0][{idx}]"

            core_fields = [
                (f"{prefix}.Enable", "true" if rule.get("enable") else "false"),
                (f"{prefix}.Name", rule.get("name", f"Rule {idx}")),
                (f"{prefix}.Type", rule.get("type", "CrossLine")),
                (f"{prefix}.Class", rule.get("type", "CrossLine")),
            ]

            for i in range(0, len(core_fields), 2):
                chunk = core_fields[i : i + 2]
                params = {"action": "setConfig", **dict(chunk)}
                resp = self._cgi_set(**params)
                if resp.strip() not in ("OK",):
                    raise DriverError(
                        f"Dahua setConfig failed for {idx}: {resp.strip()}"
                    )

            if rule.get("detect_line"):
                dl_resp = self._cgi_set(
                    action="setConfig", **{f"{prefix}.DetectLine": rule["detect_line"]}
                )
                if dl_resp.strip() not in ("OK", "Error"):
                    logger.warning(
                        "DetectLine not supported on camera %s[%d]: %s",
                        self._base_url,
                        idx,
                        dl_resp.strip(),
                    )

            if rule.get("detect_region"):
                dr_resp = self._cgi_set(
                    action="setConfig",
                    **{f"{prefix}.DetectRegion": rule["detect_region"]},
                )
                if dr_resp.strip() not in ("OK", "Error"):
                    logger.warning(
                        "DetectRegion not supported on camera %s[%d]: %s",
                        self._base_url,
                        idx,
                        dr_resp.strip(),
                    )
        return True

    def get_supported_events(self):
        return [
            "SmartMotionHuman",
            "SmartMotionVehicle",
            "SmartMotion",
            "CrossLineDetection",
            "CrossRegionDetection",
            "ParkingDetection",
            "VideoMotion",
            "AlarmLocal",
        ]

    def start_event_listener(self, callback):
        """Start background thread listening to Dahua eventManager.cgi attach.

        callback receives dicts:
            {
                "code": str,
                "action": str,   # Start / Stop / Pulse
                "index": int,
                "data": dict,
                "timestamp": str
            }

        Returns an object with a .cancel() method to stop the listener.
        """
        stop_event = threading.Event()

        class CancelContext:
            def cancel(self):
                stop_event.set()

        def _run():
            while not stop_event.wait(0.5):
                try:
                    self._event_stream(callback, stop_event)
                except Exception as e:
                    logger.warning(
                        "Dahua event stream error on %s: %s", self._base_url, e
                    )

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return CancelContext()

    def _event_stream(self, callback, stop_event):
        url = f"{self._base_url}/cgi-bin/eventManager.cgi"
        params = {
            "action": "attach",
            "codes": ["All"],
            "heartbeat": "30",
        }
        try:
            resp = self._session.get(
                url, params=params, auth=self._auth, timeout=65, stream=True
            )
            resp.raise_for_status()
            boundary = None
            for chunk in resp.iter_content(chunk_size=4096):
                if stop_event.is_set():
                    break
                if boundary is None:
                    m = re.search(rb"boundary=(\S+)", chunk)
                    if m:
                        boundary = b"--" + m.group(1)
                if boundary:
                    self._parse_event_chunk(chunk, boundary, callback)
        except requests.RequestException as e:
            raise DriverError(f"Dahua event stream failed: {e}") from e

    def _parse_event_chunk(self, chunk, boundary, callback):
        for segment in chunk.split(boundary):
            if not segment or segment == b"--" or segment == b"":
                continue
            body = segment.lstrip(b"\r\n--").strip()
            if not body:
                continue
            try:
                text = body.decode("utf-8", errors="ignore")
                self._parse_event_line(text, callback)
            except Exception:
                pass

    def _parse_event_line(self, text, callback):
        code_m = re.search(r"Code=([^;]+)", text)
        action_m = re.search(r";action=([^;]+)", text)
        index_m = re.search(r";index=(\d+)", text)
        data_m = re.search(r";data=(\{.*\})", text)
        if not code_m:
            return
        code = code_m.group(1).strip()
        action = action_m.group(1).strip() if action_m else "Unknown"
        index = int(index_m.group(1)) if index_m else 0
        data = {}
        if data_m:
            try:
                data = json.loads(data_m.group(1))
            except json.JSONDecodeError:
                pass
        callback(
            {
                "code": code,
                "action": action,
                "index": index,
                "data": data,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    # ------------------------------------------------------------------
    # CGI helpers
    # ------------------------------------------------------------------

    def _cgi_event_get(self, action, params=None):
        """GET from Dahua's eventManager.cgi. Raises DriverError on failure."""
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
        """GET from Dahua's configManager.cgi. Raises DriverError on failure."""
        url = f"{self._base_url}/cgi-bin/configManager.cgi"
        merged = {"action": action}
        merged.update(params)
        try:
            resp = self._session.get(url, params=merged, auth=self._auth, timeout=10)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            raise DriverError(f"Dahua CGI GET failed: {e}") from e

    def _cgi_set(self, **params):
        """Set config via GET request with query parameters.

        Dahua's setConfig uses query params, not POST body:
            configManager.cgi?action=setConfig&VideoAnalyseRule[0][0].Enable=true
        """
        url = f"{self._base_url}/cgi-bin/configManager.cgi"
        try:
            resp = self._session.get(url, params=params, auth=self._auth, timeout=10)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            raise DriverError(f"Dahua setConfig failed: {e}") from e

    def _cgi_post(self, action, body_lines):
        """POST to Dahua's configManager.cgi. Raises DriverError on failure."""
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
        table.VideoAnalyseRule[0][0].Enable=false

    Dahua uses two styles:
      - dots + single-bracket segments: MotionDetect[0].Window[0].Field
      - double-bracket in field name:  VideoAnalyseRule[0][0].Field
        (the [0][0] part is a single path segment with TWO indices)

    We handle both by splitting on the LAST dot to separate field name
    from the path, then parsing each segment of the path.
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

        last_dot = key.rfind(".")
        if last_dot == -1:
            continue
        path_part = key[:last_dot]
        field_name = key[last_dot + 1 :]

        segment_strs = path_part.split(".")
        obj = result
        for seg_idx, seg in enumerate(segment_strs):
            m = SEGMENT_RE.match(seg)
            if not m:
                break
            name = m.group(1)
            bracket_str = m.group(2)
            if bracket_str:
                indices = [int(x) for x in re.findall(r"\[(\d+)\]", bracket_str)]
            else:
                indices = []

            is_last_seg = seg_idx == len(segment_strs) - 1

            if not indices:
                if is_last_seg:
                    obj[name] = {}
                    obj = obj[name]
                else:
                    if name not in obj:
                        obj[name] = {}
                    obj = obj[name]
            else:
                if is_last_seg:
                    target = obj
                    for ji, idx in enumerate(indices[:-1]):
                        _ensure_list(target, name, idx)
                        target = target[name][idx]
                    last_idx = indices[-1]
                    _ensure_list(target, name, last_idx)
                    target[name][last_idx][field_name] = value
                    break
                else:
                    target = obj
                    for ji, idx in enumerate(indices):
                        _ensure_list(target, name, idx)
                        target = target[name][idx]
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
