from onvif_utils.drivers.base import CameraDriver
from onvif_utils.drivers.dahua import DahuaDriver


def get_driver(device):
    """Select and return the appropriate CameraDriver for a device by manufacturer.

    Currently always returns DahuaDriver regardless of manufacturer;
    this is the extension point for future driver implementations.
    """
    manufacturer = (device.manufacturer or "").lower()
    if "dahua" in manufacturer or "dahu" in manufacturer:
        return DahuaDriver(device)
    return DahuaDriver(device)
