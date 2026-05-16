import ipaddress
import json
import logging
import os
import socket
import threading

import nmap
import requests
from flask import Flask, jsonify, request
from wsdiscovery import WSDiscovery

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("discovery-service")

app = Flask(__name__)

DISCOVERY_PORT = int(os.environ.get("DISCOVERY_PORT", 8765))
PROBE_TIMEOUT = 3
NMAP_ARGS = "-T4 --open"


EXCLUDED_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("10.0.0.0/8"),
]


def _get_local_subnet():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
    finally:
        s.close()
    interface = ipaddress.ip_interface(f"{local_ip}/24")
    subnet = interface.network
    for excluded in EXCLUDED_NETS:
        if subnet.overlaps(excluded):
            logger.warning(
                "Detected subnet %s is in excluded range %s", subnet, excluded
            )
            return None
    return subnet


def _probe_onvif(host, port=80):
    for probe_port in [port, 80, 8080, 443]:
        for probe_path in [
            "/onvif/device_service",
            "/onvif-service",
            "/ONVIF/device_service",
        ]:
            url = f"http://{host}:{probe_port}{probe_path}"
            try:
                r = requests.get(url, timeout=PROBE_TIMEOUT)
                if r.status_code < 500:
                    return {
                        "found": True,
                        "url": url,
                        "port": probe_port,
                        "path": probe_path,
                    }
            except requests.RequestException:
                continue
    return {"found": False}


def _wsdiscovery_scan(timeout):
    devices = []
    try:
        wsd = WSDiscovery()
        wsd.start()
        services = wsd.searchServices(timeout=timeout)
        wsd.stop()

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
    except Exception as e:
        logger.warning("WS-Discovery error: %s", e)

    for d in devices:
        d["source"] = "wsdiscovery"
    return devices


def _nmap_scan(timeout):
    devices = []
    try:
        subnet = _get_local_subnet()
        if not subnet:
            logger.warning("No valid subnet detected, skipping Nmap scan")
            return devices
        logger.info("Nmap scanning subnet %s", subnet)
        nm = nmap.PortScanner()
        nm.scan(
            hosts=str(subnet),
            ports="80,8080,443,554",
            arguments=f"{NMAP_ARGS} --host-timeout {int(timeout) * 1000}ms",
        )

        for host in nm.all_hosts():
            result = _probe_onvif(host)
            if result["found"]:
                devices.append(
                    {
                        "xaddrs": [result["url"]],
                        "scopes": [],
                        "types": [],
                        "epr": "",
                        "name": "",
                        "hardware": "",
                        "profiles": [],
                        "source": "nmap",
                        "host_probed": host,
                    }
                )
                logger.info("Nmap found ONVIF device at %s", host)
    except Exception as e:
        logger.warning("Nmap scan error: %s", e)

    return devices


def _merge_devices(wsd_devices, nmap_devices):
    seen = set()
    merged = []

    for d in wsd_devices + nmap_devices:
        addrs = d.get("xaddrs", [])
        if "__all__" in d:  # safeguard
            continue
        key = None
        # Use IP from xaddrs as dedup key
        for xaddr in addrs:
            try:
                from urllib.parse import urlparse

                parsed = urlparse(xaddr)
                key = parsed.hostname
                break
            except Exception:
                continue
        # Fallback: for nmap devices use host_probed
        if not key and d.get("host_probed"):
            key = d["host_probed"]
        if not key:
            key = d.get("epr", "")

        if key and key in seen:
            # Nmap already has it, enrich if wsdiscovery has more data
            existing = next((x for x in merged if x.get("_key") == key), None)
            if existing and d.get("source") == "wsdiscovery":
                if d.get("name"):
                    existing["name"] = d["name"]
                if d.get("hardware"):
                    existing["hardware"] = d["hardware"]
                if d.get("profiles"):
                    existing["profiles"] = d["profiles"]
                existing["source"] = "both"
            continue

        if key:
            d["_key"] = key
            seen.add(key)
        merged.append(d)

    # Remove internal _key before returning
    for d in merged:
        d.pop("_key", None)
        d.pop("host_probed", None)

    return merged


@app.route("/discover", methods=["GET"])
def discover():
    timeout = request.args.get("timeout", 10, type=int)

    wsd_result = []
    nmap_result = []

    threads = []

    def do_wsd():
        nonlocal wsd_result
        wsd_result = _wsdiscovery_scan(timeout)

    def do_nmap():
        nonlocal nmap_result
        nmap_result = _nmap_scan(timeout)

    t1 = threading.Thread(target=do_wsd)
    t2 = threading.Thread(target=do_nmap)
    threads.append(t1)
    threads.append(t2)

    t1.start()
    t2.start()

    for t in threads:
        t.join()

    merged = _merge_devices(wsd_result, nmap_result)
    logger.info(
        "Discovery complete: %d WS-Discovery + %d Nmap = %d merged",
        len(wsd_result),
        len(nmap_result),
        len(merged),
    )
    return jsonify(merged)


@app.route("/probe", methods=["GET"])
def probe():
    host = request.args.get("host", "").strip()
    port = request.args.get("port", 80, type=int)

    if not host:
        return jsonify({"error": "host required"}), 400

    result = _probe_onvif(host, port)
    result["host"] = host
    result["port"] = port
    return jsonify(result)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    logger.info("Starting discovery service on port %d", DISCOVERY_PORT)
    app.run(host="0.0.0.0", port=DISCOVERY_PORT)
