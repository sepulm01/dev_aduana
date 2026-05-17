from abc import ABC, abstractmethod


class CameraDriver(ABC):
    """Abstract base class for camera-specific driver implementations.

    Subclasses implement motion detection config read/write, capability
    detection, and motion event polling via the camera's native API.
    """

    def __init__(self, device):
        self.device = device

    @abstractmethod
    def detect(self):
        """Return a string identifying the driver (e.g. 'dahua', 'onvif_std')."""
        ...

    @abstractmethod
    def get_motion_config(self):
        """Read current motion detection configuration from the camera.

        Returns a dict with the camera-specific motion settings.
        Raises DriverError on failure.
        """
        ...

    @abstractmethod
    def set_motion_config(self, config):
        """Write motion detection configuration to the camera.

        Args:
            config: dict with the same structure returned by get_motion_config().

        Returns True on success.
        Raises DriverError on failure.
        """
        ...

    @abstractmethod
    def get_capabilities(self):
        """Return a dict describing what this driver/camera supports.

        Example:
            {"motion_detection": True, "windows": 4, "region_bitmask": True}
        """
        ...

    def poll_motion(self):
        """Poll the camera for current motion detection status.

        Returns a dict with:
            "motion": bool
            "timestamp": str  (ISO 8601)
            "metadata": dict  (optional extra info)

        Returns None if this driver does not support polling.
        """
        return None

    def get_ivs_rules(self):
        """Read IVS rules configuration from the camera.

        Returns a list of rule dicts. Each dict contains at minimum:
            {"name": str, "enable": bool, "type": str, "detections": list}

        Returns [] if IVS is not supported.
        Raises DriverError on failure.
        """
        return []

    def set_ivs_rules(self, rules):
        """Write IVS rules configuration to the camera.

        Args:
            rules: list of rule dicts, same structure as returned by get_ivs_rules().

        Returns True on success.
        Raises DriverError on failure.
        """
        return False

    def get_supported_events(self):
        """Return list of event codes this camera/driver can stream.

        Example: ["SmartMotionHuman", "SmartMotionVehicle", "CrossLineDetection"]
        """
        return []

    def start_event_listener(self, callback):
        """Start a background event listener for this camera.

        Args:
            callback: callable that receives event dicts:
                {
                    "code": str,       # event code like "SmartMotionHuman"
                    "action": str,     # "Start", "Stop", "Pulse"
                    "index": int,      # channel index
                    "data": dict,      # event-specific metadata (Rect, Object IDs, etc)
                    "timestamp": str   # ISO 8601
                }

        Returns a cancellable context (call .cancel() to stop).

        IMPORTANT: This must be non-blocking. The implementation should
        spawn a background thread or use asyncio internally so it does
        not block the calling thread.

        Returns None if this driver does not support event listeners.
        """
        return None

    def ping(self):
        """Check if the camera is reachable and responsive.

        Returns a dict with:
            "online": bool
            "last_seen": datetime or None  (UTC)

        Returns {"online": False, "last_seen": None} on failure.
        """
        return {"online": False, "last_seen": None}


class DriverError(Exception):
    """Raised on driver-level failures (network, auth, unexpected responses)."""
