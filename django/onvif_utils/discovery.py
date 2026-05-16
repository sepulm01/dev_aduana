import requests
from wsdiscovery import WSDiscovery


class DeviceDiscovery:
    def __init__(self, timeout=10):
        self.timeout = timeout
        self._devices = []

    def discover(self, timeout=None):
        if timeout is not None:
            self.timeout = timeout

        wsd = WSDiscovery()
        wsd.start()
        services = wsd.searchServices(timeout=self.timeout)
        wsd.stop()

        devices = []
        for service in services:
            xaddrs = list(service.getXAddrs())
            scopes = list(service.getScopes())
            types = list(service.getTypes())

            device = {
                "xaddrs": xaddrs,
                "scopes": scopes,
                "types": types,
                "epr": str(service.getEPR()) if service.getEPR() else "",
            }

            for scope in scopes:
                if scope.startswith("onvif://www.onvif.org/name/"):
                    device["name"] = scope.split("/")[-1]
                if scope.startswith("onvif://www.onvif.org/hardware/"):
                    device["hardware"] = scope.split("/")[-1]
                if scope.startswith("onvif://www.onvif.org/Profile/"):
                    device.setdefault("profiles", []).append(scope.split("/")[-1])

            devices.append(device)

        self._devices = devices
        return devices

    @staticmethod
    def probe_ip(host, port=80):
        result = {"host": host, "port": port, "found": False}

        for probe_port in [port, 80, 8080, 443]:
            for probe_path in [
                "/onvif/device_service",
                "/onvif-service",
                "/ONVIF/device_service",
            ]:
                url = f"http://{host}:{probe_port}{probe_path}"
                try:
                    r = requests.get(url, timeout=3)
                    if r.status_code < 500:
                        result.update(
                            {
                                "found": True,
                                "url": url,
                                "port": probe_port,
                                "path": probe_path,
                            }
                        )
                        return result
                except requests.RequestException:
                    continue
        return result
