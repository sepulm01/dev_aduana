import asyncio
import concurrent.futures
import logging

from devices.models import Device

logger = logging.getLogger(__name__)


def collect_snmp_metrics():
    try:
        devices = list(Device.objects.filter(snmp_enabled=True))
        if not devices:
            return {"devices": []}

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_run_async_collect, devices)
            return future.result(timeout=30)
    except Exception as e:
        logger.warning("SNMP collection error: %s", e)
        return {"error": str(e)}


def _run_async_collect(devices):
    return asyncio.run(_async_collect(devices))


async def _async_collect(devices):
    from pysnmp.hlapi import v1arch

    results = []

    for device in devices:
        community = device.snmp_community or "public"
        target = (device.host or device.ip_address, device.snmp_port or 161)

        sys_name = await _snmp_get(v1arch, target, community, "1.3.6.1.2.1.1.5.0")
        sys_descr = await _snmp_get(v1arch, target, community, "1.3.6.1.2.1.1.1.0")
        uptime = await _snmp_get(v1arch, target, community, "1.3.6.1.2.1.1.3.0")

        results.append(
            {
                "device_id": device.id,
                "device_name": device.name,
                "host": device.host or device.ip_address,
                "sys_name": sys_name or sys_descr,
                "sys_descr": sys_descr,
                "uptime_seconds": _parse_uptime(uptime),
            }
        )

    return {"devices": results}


async def _snmp_get(v1arch, target, community, oid):
    dispatcher = v1arch.SnmpDispatcher()
    try:
        transport = await v1arch.UdpTransportTarget.create(target, timeout=2, retries=0)
        error_indication, error_status, error_index, var_binds = await v1arch.get_cmd(
            dispatcher,
            v1arch.CommunityData(community, mpModel=1),
            transport,
            v1arch.ObjectType(v1arch.ObjectIdentity(oid)),
        )
        if error_indication:
            logger.debug("SNMP %s %s: %s", target, oid, error_indication)
            return None
        if error_status:
            logger.debug("SNMP %s %s: status=%s", target, oid, error_status)
            return None
        if var_binds:
            for var_bind in var_binds:
                val = str(var_bind[1])
                if val and val != "(none)":
                    return val
    except Exception as e:
        logger.debug("SNMP %s %s: %s", target, oid, e)
    finally:
        dispatcher.close()
    return None


def _parse_uptime(value):
    if value is None:
        return None
    try:
        ticks = int(value) / 100
        return round(ticks, 0)
    except (ValueError, TypeError):
        return None
