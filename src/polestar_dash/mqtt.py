"""MQTT publisher with optional Home Assistant auto-discovery."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict

from .config import get_settings
from .polestar.fetcher import Snapshot

log = logging.getLogger(__name__)

_published_discovery: set[str] = set()  # vin → already announced
# Strong refs to in-flight publish tasks. Without this, Python is free to GC a
# pending Task while still scheduled (asyncio only weak-refs them), and the
# publish silently disappears.
_pending: set[asyncio.Task] = set()


def _client_kwargs() -> dict | None:
    s = get_settings()
    if not s.mqtt_host:
        return None
    kw: dict = {"hostname": s.mqtt_host, "port": s.mqtt_port}
    if s.mqtt_username:
        kw["username"] = s.mqtt_username
    if s.mqtt_password:
        kw["password"] = s.mqtt_password
    return kw


def _sensor(_id: str, name: str, unit: str | None, dc: str | None, sc: str | None) -> dict:
    return {"id": _id, "name": name, "unit": unit, "dc": dc, "sc": sc}


_SENSORS: list[dict] = [
    _sensor("soc_percent", "State of charge", "%", "battery", "measurement"),
    _sensor("range_km", "Range", "km", "distance", "measurement"),
    _sensor("charging_power_w", "Charging power", "W", "power", "measurement"),
    _sensor("charging_current_a", "Charging current", "A", "current", "measurement"),
    _sensor("minutes_to_full", "Minutes to full", "min", None, "measurement"),
    _sensor("odometer_km", "Odometer", "km", "distance", "total_increasing"),
    _sensor("avg_consumption_kwh_per_100km", "Avg consumption", "kWh/100km", None, "measurement"),
    _sensor("days_to_service", "Days to service", "d", None, "measurement"),
    _sensor("distance_to_service_km", "Distance to service", "km", "distance", "measurement"),
    _sensor("charging_status", "Charging status", None, None, None),
    _sensor("charger_connection_status", "Plug status", None, None, None),
    _sensor("service_warning", "Service warning", None, None, None),
    _sensor("charging_voltage_v", "Charging voltage", "V", "voltage", "measurement"),
    _sensor("charging_type", "Charging type", None, None, None),
    _sensor("target_soc_percent", "Charge limit", "%", "battery", "measurement"),
    _sensor("target_soc_type", "Charge limit type", None, None, None),
    _sensor("minutes_to_minimum_soc", "Minutes to minimum SoC", "min", None, "measurement"),
]


def _discovery_payload(vin: str, base: str, sensor: dict) -> tuple[str, dict]:
    object_id = f"poledash_{vin}_{sensor['id']}"
    topic = f"homeassistant/sensor/{object_id}/config"
    payload = {
        "name": sensor["name"],
        "unique_id": object_id,
        "object_id": object_id,
        "state_topic": f"{base}/{vin}/state",
        "value_template": "{{ value_json." + sensor["id"] + " }}",
        "availability_topic": f"{base}/{vin}/availability",
        "device": {
            "identifiers": [f"poledash_{vin}"],
            "manufacturer": "Polestar",
            "name": f"Polestar ({vin[-6:]})",
            "model": "Polestar 2",
            "via_device": "poledash",
        },
    }
    if sensor.get("unit"):
        payload["unit_of_measurement"] = sensor["unit"]
    if sensor.get("dc"):
        payload["device_class"] = sensor["dc"]
    if sensor.get("sc"):
        payload["state_class"] = sensor["sc"]
    return topic, payload


async def publish_snapshot(snap: Snapshot) -> None:
    """Publish a snapshot + HA discovery (once per VIN per process) over MQTT."""
    kw = _client_kwargs()
    if kw is None:
        return

    s = get_settings()
    base = s.mqtt_base_topic.rstrip("/")
    vin = snap.vin

    try:
        import aiomqtt

        async with aiomqtt.Client(**kw) as client:
            if s.mqtt_ha_discovery and vin not in _published_discovery:
                for sensor in _SENSORS:
                    topic, payload = _discovery_payload(vin, base, sensor)
                    await client.publish(topic, json.dumps(payload), retain=True)
                _published_discovery.add(vin)
                log.info("Published HA discovery for %s", vin)

            data = asdict(snap)
            data.pop("raw", None)
            for k, v in list(data.items()):
                if hasattr(v, "isoformat"):
                    data[k] = v.isoformat()
            await client.publish(f"{base}/{vin}/state", json.dumps(data), retain=True)
            await client.publish(f"{base}/{vin}/availability", "online", retain=True)
    except Exception:  # noqa: BLE001
        log.exception("MQTT publish failed for VIN %s", vin)


def schedule_publish(snap: Snapshot) -> None:
    """Fire-and-forget MQTT publish from the scheduler loop."""
    if _client_kwargs() is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("schedule_publish called outside an event loop; dropping")
        return
    task = loop.create_task(publish_snapshot(snap))
    _pending.add(task)

    def _done(t: asyncio.Task) -> None:
        _pending.discard(t)
        if not t.cancelled() and t.exception() is not None:
            # publish_snapshot already logs; this just consumes the exception
            # so it doesn't surface as "Task exception was never retrieved".
            pass

    task.add_done_callback(_done)
