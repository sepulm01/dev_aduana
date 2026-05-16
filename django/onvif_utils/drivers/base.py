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


class DriverError(Exception):
    """Raised on driver-level failures (network, auth, unexpected responses)."""
