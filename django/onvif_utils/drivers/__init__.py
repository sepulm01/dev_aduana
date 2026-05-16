from onvif_utils.drivers.base import CameraDriver
from onvif_utils.drivers.dahua import DahuaDriver


def get_driver(device):
    manufacturer = (device.manufacturer or "").lower()
    if "dahua" in manufacturer or "dahu" in manufacturer:
        return DahuaDriver(device)
    return DahuaDriver(device)
