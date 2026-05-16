from datetime import datetime, timezone

from onvif import ONVIFCamera


class OnvifClient:
    """ONVIF device client wrapping onvif-zeep with lazy service initialization.

    Provides properties to access Device, Media, and PTZ services,
    plus convenience methods to read capabilities, video sources,
    network config, and system time.
    """

    def __init__(self, host, port, username, password):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._cam = ONVIFCamera(host, port, username, password, adjust_time=True)
        self._device = None
        self._media = None
        self._ptz = None

    @property
    def device(self):
        """Lazy-init DeviceMgmt SOAP service."""
        if self._device is None:
            self._device = self._cam.devicemgmt
        return self._device

    @property
    def media(self):
        """Lazy-init Media SOAP service."""
        if self._media is None:
            self._media = self._cam.create_media_service()
        return self._media

    @property
    def ptz(self):
        """Lazy-init PTZ SOAP service."""
        if self._ptz is None:
            self._ptz = self._cam.create_ptz_service()
        return self._ptz

    def get_device_info(self):
        """Return manufacturer, model, firmware, serial_number, hardware_id."""
        info = self.device.GetDeviceInformation()
        return {
            "manufacturer": info.Manufacturer,
            "model": info.Model,
            "firmware": info.FirmwareVersion,
            "serial_number": info.SerialNumber,
            "hardware_id": info.HardwareId,
        }

    def get_capabilities(self):
        """Return network/media/ptz/events/imaging capabilities dict."""
        caps = self.device.GetCapabilities({"Category": ["All"]})
        return {
            "network": caps.Network if hasattr(caps, "Network") else {},
            "media": caps.Media if hasattr(caps, "Media") else {},
            "ptz": caps.PTZ if hasattr(caps, "PTZ") else {},
            "events": caps.Events if hasattr(caps, "Events") else {},
            "imaging": caps.Imaging if hasattr(caps, "Imaging") else {},
        }

    def get_services(self):
        """Return ONVIF service profile support (S/T/M/G/Q) and namespace list."""
        services = self.device.GetServices({"IncludeCapability": True})
        namespaces = [s.Namespace for s in services if hasattr(s, "Namespace")]
        return {
            "profiles": {
                "S": "http://www.onvif.org/ver10/media/wsdl" in namespaces,
                "T": "http://www.onvif.org/ver20/media/wsdl" in namespaces,
                "M": "http://www.onvif.org/ver20/analytics/wsdl" in namespaces,
                "G": "http://www.onvif.org/ver10/recording/wsdl" in namespaces,
                "Q": "http://www.onvif.org/ver10/deviceIO/wsdl" in namespaces,
            },
            "services": namespaces,
        }

    def get_video_sources(self):
        """Return max width, height, and framerate from the first video source."""
        try:
            sources = self.media.GetVideoSources()
            if sources:
                s = sources[0]
                res = (
                    s.Resolution if hasattr(s, "Resolution") and s.Resolution else None
                )
                return {
                    "max_width": res.Width if res else None,
                    "max_height": res.Height if res else None,
                    "max_framerate": getattr(s, "Framerate", None),
                }
        except Exception:
            pass
        return {}

    def get_media_capabilities(self):
        """Return media service capabilities (snapshot, osd, rotation, rtp)."""
        try:
            caps = self.media.GetServiceCapabilities()
            streaming = getattr(caps, "StreamingCapabilities", None) or {}
            return {
                "snapshot_uri": bool(getattr(caps, "SnapshotUri", False)),
                "osd": bool(getattr(caps, "OSD", False)),
                "rotation": bool(getattr(caps, "Rotation", False)),
                "max_profiles": getattr(
                    getattr(caps, "ProfileCapabilities", None) or {},
                    "MaximumNumberOfProfiles",
                    None,
                ),
                "rtp_multicast": bool(getattr(streaming, "RTPMulticast", False)),
                "rtp_tcp": bool(getattr(streaming, "RTP_TCP", False)),
            }
        except Exception:
            return {}

    def get_ptz_capabilities(self):
        """Return PTZ service capabilities (eflip, reverse)."""
        try:
            caps = self.ptz.GetServiceCapabilities()
            return {
                "eflip": bool(getattr(caps, "EFlip", False)),
                "reverse": bool(getattr(caps, "Reverse", False)),
            }
        except Exception:
            return {}

    def get_network_interfaces(self):
        """Return list of network interfaces with MAC, IP, DHCP, MTU."""
        try:
            ifaces = self.device.GetNetworkInterfaces()
            result = []
            for iface in ifaces:
                info = getattr(iface, "Info", None)
                ipv4 = getattr(iface, "IPv4", None)
                config = getattr(ipv4, "Config", None) if ipv4 else None
                manual = getattr(config, "Manual", None) if config else []
                ip = ""
                if manual:
                    entry = manual[0]
                    ip = getattr(entry, "Address", "") or ""
                dhcp = not (getattr(config, "DHCP", True) if config else True)
                result.append(
                    {
                        "name": getattr(info, "Name", "") if info else "",
                        "mac": getattr(info, "HwAddress", "") if info else "",
                        "ip": ip,
                        "dhcp": dhcp,
                        "mtu": getattr(info, "MTU", None) if info else None,
                    }
                )
            return result
        except Exception:
            return []

    def get_hostname(self):
        """Return camera hostname string."""
        try:
            hn = self.device.GetHostname()
            return getattr(hn, "Name", "") or ""
        except Exception:
            return ""

    def get_dns(self):
        """Return list of DNS server IPs configured on the camera."""
        try:
            dns = self.device.GetDNS()
            manual = getattr(dns, "DNSManual", None) or []
            servers = []
            for s in manual:
                ip4 = getattr(s, "IPv4Address", None) or ""
                ip6 = getattr(s, "IPv6Address", None) or ""
                servers.append(ip4 or ip6)
            return servers
        except Exception:
            return []

    def get_ntp(self):
        """Return list of NTP server hostnames configured on the camera."""
        try:
            ntp = self.device.GetNTP()
            manual = getattr(ntp, "NTPManual", None) or []
            servers = []
            for s in manual:
                name = getattr(s, "DNSname", None) or ""
                servers.append(name)
            return servers
        except Exception:
            return []

    def get_system_date_time(self):
        """Return camera's system time config (type, timezone, DST)."""
        try:
            dt = self.device.GetSystemDateAndTime()
            return {
                "type": getattr(dt, "DateTimeType", ""),
                "tz": getattr(getattr(dt, "TimeZone", None) or {}, "TZ", ""),
                "dst": bool(getattr(dt, "DaylightSavings", False)),
            }
        except Exception:
            return {}

    def set_system_date_time(self, utc_dt=None, tz=None, dst=False):
        """Set the camera's system date/time to a UTC datetime."""
        now = utc_dt or datetime.now(timezone.utc)
        params = {
            "DateTimeType": "Manual",
            "DaylightSavings": dst,
            "UTCDateTime": {
                "Time": {
                    "Hour": now.hour,
                    "Minute": now.minute,
                    "Second": now.second,
                },
                "Date": {
                    "Year": now.year,
                    "Month": now.month,
                    "Day": now.day,
                },
            },
        }
        if tz:
            params["TimeZone"] = {"TZ": tz}
        return self.device.SetSystemDateAndTime(params)
