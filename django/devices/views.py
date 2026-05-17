import json
import logging
import os
from datetime import datetime, timezone

import requests

from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from devices.models import Device, IVSRule
from live.views import DEFAULT_CAMERA_SPECS
from onvif_utils.client import OnvifClient
from onvif_utils.discovery import DeviceDiscovery
from onvif_utils.drivers import get_driver
from onvif_utils.drivers.base import DriverError
from onvif_utils.media import MediaService
from onvif_utils.mediamtx_api import MediaMTXAPI

logger = logging.getLogger(__name__)


def dashboard(request):
    devices = Device.objects.all()
    return render(request, "devices/dashboard.html", {"devices": devices})


def device_detail(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    from live.views import build_stream_context

    profile_token = device.default_profile_token or ""
    ctx = build_stream_context(device, profile_token, request.get_host())
    ctx["device"] = device
    ctx["profile_token"] = profile_token
    ctx["camera_specs_json"] = json.dumps(device.camera_specs or {})
    return render(request, "devices/device_detail.html", ctx)


DISCOVERY_SERVICE_URL = os.environ.get("DISCOVERY_SERVICE_URL", "http://localhost:8765")


@csrf_exempt
def discover(request):
    if request.method == "POST":
        timeout = int(request.POST.get("timeout", 10))
        try:
            devices = DeviceDiscovery.discover_remote(
                base_url=DISCOVERY_SERVICE_URL, timeout=timeout
            )
        except requests.RequestException:
            devices = DeviceDiscovery(timeout=timeout).discover()
        return JsonResponse(devices, safe=False)
    return render(request, "devices/discover.html")


@csrf_exempt
def probe(request):
    if request.method == "POST":
        data = json.loads(request.body)
        host = data.get("host", "").strip()
        port = int(data.get("port", 80))
        if not host:
            return JsonResponse({"error": "host required"}, status=400)
        try:
            result = DeviceDiscovery.probe_remote(
                host=host, port=port, base_url=DISCOVERY_SERVICE_URL
            )
        except requests.RequestException:
            result = DeviceDiscovery.probe_ip(host, port)
        return JsonResponse(result)
    return JsonResponse({"error": "POST required"}, status=405)


@csrf_exempt
def add_device(request):
    if request.method == "POST":
        data = json.loads(request.body)
        host = data.get("host", "")
        port = int(data.get("port", 80))
        username = data.get("username", "")
        password = data.get("password", "")

        if not username or not password:
            return JsonResponse(
                {"error": "username and password are required"},
                status=400,
            )

        try:
            client = OnvifClient(host, port, username, password)
            info = client.get_device_info()
            manufacturer = info["manufacturer"]
            model = info["model"]
            firmware = info["firmware"]
            serial_number = info["serial_number"]
            hardware_id = info["hardware_id"]
        except Exception as e:
            return JsonResponse(
                {"error": f"ONVIF connection failed: {e}"},
                status=400,
            )

        device = Device.objects.create(
            name=data.get("name", host or ""),
            host=host,
            port=port,
            username=username,
            password=password,
            manufacturer=manufacturer,
            model=model,
            firmware=firmware,
            serial_number=serial_number,
            hardware_id=hardware_id,
            xaddrs=json.dumps(data.get("xaddrs", [])),
            scopes=json.dumps(data.get("scopes", [])),
            is_online=True,
            camera_specs=dict(DEFAULT_CAMERA_SPECS),
        )

        if username and password:
            try:
                svc = MediaService(client)
                profiles = svc.get_profiles()
                profiles_tokens = []
                stream_uris = []
                device.stream_uris = {}
                for p in profiles:
                    uri = svc.get_stream_uri(
                        p["token"], username=device.username, password=device.password
                    )
                    if uri:
                        profiles_tokens.append(p["token"])
                        stream_uris.append(uri)
                        device.stream_uris[p["token"]] = uri

                mtx = MediaMTXAPI()
                mtx.ensure_camera_streams(device.id, profiles_tokens, stream_uris)
            except Exception as e:
                logger.warning(
                    "Error setting up streams for device %s: %s", device.id, e
                )

        return JsonResponse({"ok": True, "id": device.id})
    return JsonResponse({"error": "POST required"}, status=405)


@csrf_exempt
def delete_device(request, device_id):
    if request.method == "POST":
        device = get_object_or_404(Device, id=device_id)
        mtx = MediaMTXAPI()
        mtx.delete_camera_paths(device.id)
        device.delete()
        return JsonResponse({"ok": True})
    return JsonResponse({"error": "POST required"}, status=405)


def device_profiles(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    try:
        client = OnvifClient(device.host, device.port, device.username, device.password)
        svc = MediaService(client)
        profiles = svc.get_profiles()
        for p in profiles:
            uri = svc.get_stream_uri(
                p["token"], username=device.username, password=device.password
            )
            p["stream_uri"] = uri
        return JsonResponse(profiles, safe=False)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
def scan_device(request, device_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    device = get_object_or_404(Device, id=device_id)
    try:
        client = OnvifClient(device.host, device.port, device.username, device.password)
        specs = dict(DEFAULT_CAMERA_SPECS)

        try:
            info = client.get_device_info()
            device.manufacturer = info["manufacturer"]
            device.model = info["model"]
            device.firmware = info["firmware"]
            device.serial_number = info["serial_number"]
            device.hardware_id = info["hardware_id"]
        except Exception:
            pass

        try:
            specs["onvif_profiles"] = client.get_services()
        except Exception:
            pass
        try:
            specs["video_source"] = client.get_video_sources()
        except Exception:
            pass
        try:
            specs["media_caps"] = client.get_media_capabilities()
        except Exception:
            pass
        try:
            specs["ptz_caps"] = client.get_ptz_capabilities()
        except Exception:
            pass
        try:
            net = client.get_network_interfaces()
            if net:
                specs["network"] = net[0]
        except Exception:
            pass
        try:
            specs["hostname"] = client.get_hostname()
        except Exception:
            pass
        try:
            specs["dns"] = client.get_dns()
        except Exception:
            pass
        try:
            ntp = client.get_ntp()
            if ntp:
                specs["ntp"] = ntp
        except Exception:
            pass
        try:
            specs["system_time"] = client.get_system_date_time()
        except Exception:
            pass

        specs["last_scan"] = datetime.now(timezone.utc).isoformat()
        device.camera_specs = specs
        device.save()
        return JsonResponse({"ok": True, "camera_specs": specs})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
def sync_time(request, device_id=None):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    now = datetime.now(timezone.utc)
    devices_qs = (
        Device.objects.filter(id=device_id) if device_id else Device.objects.all()
    )

    results = []
    for device in devices_qs:
        try:
            client = OnvifClient(
                device.host, device.port, device.username, device.password
            )
            specs = device.camera_specs or {}
            tz = None
            if isinstance(specs, dict) and specs.get("system_time"):
                tz = specs["system_time"].get("tz", None)
            client.set_system_date_time(utc_dt=now, tz=tz)
            if isinstance(specs, dict):
                specs["last_time_sync"] = now.isoformat()
                device.camera_specs = specs
                device.save()
            results.append({"id": device.id, "name": device.name, "ok": True})
        except Exception as e:
            results.append(
                {"id": device.id, "name": device.name, "ok": False, "error": str(e)}
            )

    return JsonResponse({"results": results})


@csrf_exempt
def set_default_profile(request, device_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    device = get_object_or_404(Device, id=device_id)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "JSON required"}, status=400)
    device.default_profile_token = data.get("profile_token", "")
    device.save()
    return JsonResponse(
        {"ok": True, "default_profile_token": device.default_profile_token}
    )


@csrf_exempt
def device_motion_config(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    driver = get_driver(device)

    if request.method == "GET":
        try:
            config = driver.get_motion_config()
            return JsonResponse(config)
        except DriverError as e:
            return JsonResponse({"error": str(e)}, status=500)

    elif request.method == "POST":
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "JSON required"}, status=400)
        try:
            driver.set_motion_config(data)
            return JsonResponse({"ok": True})
        except DriverError as e:
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"error": "GET or POST required"}, status=405)


@csrf_exempt
def device_ivs_config(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    driver = get_driver(device)

    if request.method == "GET":
        try:
            rules = driver.get_ivs_rules()
            return JsonResponse({"rules": rules})
        except DriverError as e:
            return JsonResponse({"error": str(e)}, status=500)

    elif request.method == "POST":
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "JSON required"}, status=400)

        rules = data.get("rules", [])
        camera_write_ok = False
        camera_error = None
        try:
            driver.set_ivs_rules(rules)
            camera_write_ok = True
        except DriverError as e:
            camera_error = str(e)
            if "does not support IVS config via CGI" not in camera_error:
                return JsonResponse({"error": camera_error}, status=500)

        IVSRule.objects.filter(device=device).delete()
        for idx, rule in enumerate(rules):
            IVSRule.objects.update_or_create(
                device=device,
                index=idx,
                defaults={
                    "name": rule.get("name", f"Rule {idx}"),
                    "rule_type": rule.get("type", "CrossLine"),
                    "enable": rule.get("enable", True),
                    "direction": rule.get("direction", "Both"),
                    "detect_line": rule.get("detect_line", ""),
                    "detect_region": rule.get("detect_region", ""),
                    "event_handler": rule.get("event_handler", {}),
                    "camera_rule_id": idx,
                },
            )
        if camera_write_ok:
            return JsonResponse({"ok": True})
        else:
            return JsonResponse(
                {
                    "ok": True,
                    "warning": "Rules saved locally only. Camera does not support IVS config via CGI.",
                }
            )

    return JsonResponse({"error": "GET or POST required"}, status=405)


@csrf_exempt
def device_events(request, device_id):
    device = get_object_or_404(Device, id=device_id)

    if request.method != "GET":
        return JsonResponse({"error": "GET required"}, status=405)

    limit = int(request.GET.get("limit", 100))
    events = device.events.all()[:limit]
    return JsonResponse(
        [
            {
                "id": e.id,
                "code": e.code,
                "action": e.action,
                "index": e.index,
                "data": e.data,
                "timestamp": e.timestamp.isoformat(),
            }
            for e in events
        ],
        safe=False,
    )


@csrf_exempt
def device_event_listener_toggle(request, device_id):
    device = get_object_or_404(Device, id=device_id)

    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "JSON required"}, status=400)

    enabled = bool(data.get("enable", False))
    device.event_listener_enabled = enabled
    device.save()
    return JsonResponse({"ok": True, "event_listener_enabled": enabled})
