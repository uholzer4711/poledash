"""Experimental C3 (Volvo Cloud Connectivity) client — powers the /test page.

This is DELIBERATELY isolated from the production pypolestar poll loop:
- separate library (`unofficial-polestar-api`), imported lazily so a missing or
  broken install can never take down the rest of the app,
- read-only: only `get_*` accessors are called, never a command,
- on-demand: fetched when the /test page asks, cached briefly, nothing persisted
  to the DB and no scheduler job.

It surfaces data pypolestar does not expose for our car: live GPS, exterior
door/window/lock/alarm state, climate, cabin air quality, on-car weather, and a
richer health block (light/tyre warnings).
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from ..config import get_settings

# --- enum decode tables (from the C3 protobufs; see lab research) -------------
_OPEN = {0: "—", 1: "open", 2: "closed", 3: "ajar"}
_LOCK = {0: "—", 1: "unlocked", 2: "locked"}
_ALARM = {0: "—", 1: "triggered", 2: "off"}
_WARN = {0: "—", 1: "ok", 2: "warning", 3: "too low", 4: "too high"}
_BULB = {0: "—", 1: "ok", 2: "fault"}
_CONN = {0: "—", 1: "connected", 2: "disconnected", 3: "fault"}
_CHARGING = {0: "—", 1: "charging", 2: "idle", 3: "scheduled", 4: "discharging",
             5: "error", 6: "smart charging", 7: "done", 8: "smart paused"}
_CTYPE = {0: "—", 1: "none", 2: "AC", 3: "DC", 4: "wireless"}
_HEATCOOL = {0: "—", 1: "not required", 2: "heating", 3: "cooling",
             4: "cooling (fast)", 5: "ventilation only"}
_CLIMA = {0: "—", 1: "idle", 2: "starting", 3: "starting (delay)",
          4: "external power", 5: "active", 6: "active + pre-cleaning",
          7: "pre-cleaning only", 8: "heater active", 9: "deleting timer"}
_AVAIL = {0: "—", 1: "available", 2: "unavailable"}
_SERVICE = {0: "—", 1: "no warning", 2: "unknown", 3: "maintenance soon",
            4: "engine-hours soon", 5: "distance soon", 6: "maintenance due",
            7: "engine-hours due", 8: "distance due"}

_TTL_S = 30.0  # cache window — gentle on the C3 rate limiter
_lock = asyncio.Lock()
_api: Any = None
_vehicle: Any = None
_cache: dict | None = None
_cache_at: float = 0.0


def _g(obj: Any, *names: str, default=None):
    """getattr through a chain, tolerating missing attrs."""
    for n in names:
        if obj is None:
            return default
        obj = getattr(obj, n, None)
    return obj if obj is not None else default


def _coord(loc: Any) -> dict | None:
    c = _g(loc, "coordinate")
    lat, lng = _g(c, "latitude"), _g(c, "longitude")
    if lat is None or lng is None or (lat == 0 and lng == 0):
        return None
    return {
        "lat": float(lat),
        "lng": float(lng),
        "altitude": _g(loc, "altitude"),
        "heading": _g(loc, "heading"),
        "speed": _g(loc, "speed"),
        "ts": _g(loc, "timestamp", "seconds"),
    }


def _door(d: Any) -> dict:
    return {
        "open": _OPEN.get(int(_g(d, "open_status", default=0)), "?"),
        "lock": _LOCK.get(int(_g(d, "lock_status", default=0)), "?"),
        "alarm": _ALARM.get(int(_g(d, "alarm_status", default=0)), "?"),
    }


async def _ensure_client():
    global _api, _vehicle
    if _api is not None and _vehicle is not None:
        return
    from polestar_api import PolestarApi  # lazy

    s = get_settings()
    api = PolestarApi(email=s.polestar_email, password=s.polestar_password)
    await api.async_init()
    vehicles = await api.get_vehicles()
    if not vehicles:
        await _safe_close(api)
        raise RuntimeError("C3 returned no vehicles")
    _api, _vehicle = api, vehicles[0]


async def _safe_close(api: Any):
    with contextlib.suppress(Exception):
        await api.close()


async def _reset():
    global _api, _vehicle
    if _api is not None:
        await _safe_close(_api)
    _api, _vehicle = None, None


async def _collect() -> dict:
    car = _vehicle
    out: dict[str, Any] = {
        "vehicle": {
            "model": _g(car, "model_name"),
            "model_year": _g(car, "model_year"),
            "vin": _g(car, "vin"),
            "registration_no": _g(car, "registration_no"),
        }
    }

    async def grab(key, meth, build):
        fn = getattr(car, meth, None)
        if fn is None:
            return
        try:
            r = await asyncio.wait_for(fn(), timeout=25)
            out[key] = build(r)
        except Exception as e:
            out.setdefault("_errors", {})[key] = f"{type(e).__name__}: {e}"[:160]

    await grab("location", "get_location", _coord)
    await grab("parked", "get_parked_location", _coord)
    await grab("weather", "get_weather", lambda r: {
        "temp_c": _g(r, "temperature_celsius"),
        "ts_ms": _g(r, "timestamp_epoch_millis"),
    })
    await grab("exterior", "get_exterior", lambda r: {
        "is_locked": bool(_g(r, "is_locked", default=False)),
        "any_door_open": bool(_g(r, "any_door_open", default=False)),
        "doors": {
            "front_left": _door(_g(r, "doors", "front_left")),
            "front_right": _door(_g(r, "doors", "front_right")),
            "rear_left": _door(_g(r, "doors", "rear_left")),
            "rear_right": _door(_g(r, "doors", "rear_right")),
        },
        "windows": {
            "front_left": _OPEN.get(int(_g(r, "windows", "front_left", "open_status", default=0)), "?"),
            "front_right": _OPEN.get(int(_g(r, "windows", "front_right", "open_status", default=0)), "?"),
            "rear_left": _OPEN.get(int(_g(r, "windows", "rear_left", "open_status", default=0)), "?"),
            "rear_right": _OPEN.get(int(_g(r, "windows", "rear_right", "open_status", default=0)), "?"),
        },
        "sunroof": _OPEN.get(int(_g(r, "sunroof", "open_status", default=0)), "?"),
        "hood": _OPEN.get(int(_g(r, "hood", "status", "open_status", default=0)), "?"),
        "tailgate": _OPEN.get(int(_g(r, "tailgate", "status", "open_status", default=0)), "?"),
        "tank_lid": _OPEN.get(int(_g(r, "tank_lid", "open_status", default=0)), "?"),
        "central_lock": _LOCK.get(int(_g(r, "central_lock", "lock_status", default=0)), "?"),
    })
    await grab("climate", "get_climate", lambda r: {
        "is_active": bool(_g(r, "is_active", default=False)),
        "action": _HEATCOOL.get(int(_g(r, "heat_or_cool_action", default=0)), "?"),
        "running": _CLIMA.get(int(_g(r, "running_status", default=0)), "?"),
        "time_remaining": _g(r, "time_remaining"),
    })
    await grab("air", "get_precleaning", lambda r: {
        "is_running": bool(_g(r, "is_running", default=False)),
        "aqi": _g(r, "measured_air_quality_index"),
        "pm25": _g(r, "measured_particulate_matter_2_5"),
        "last_cycle_valid": bool(_g(r, "last_cycle_valid", default=False)),
    })
    await grab("health", "get_health", lambda r: {
        "any_light_failure": bool(_g(r, "any_light_failure", default=False)),
        "any_tyre_warning": bool(_g(r, "any_tyre_warning", default=False)),
        "service_warning": _SERVICE.get(int(_g(r, "service_warning", default=0)), "?"),
        "days_to_service": _g(r, "days_to_service"),
        "distance_to_service_km": _g(r, "distance_to_service_km"),
        "brake_fluid": _WARN.get(int(_g(r, "brake_fluid_level_warning", default=0)), "?"),
        "oil": _WARN.get(int(_g(r, "oil_level_warning", default=0)), "?"),
        "coolant": _WARN.get(int(_g(r, "engine_coolant_level_warning", default=0)), "?"),
    })
    await grab("battery", "get_battery", lambda r: {
        "charge_level": _g(r, "charge_level"),
        "range_km": _g(r, "range_km"),
        "is_plugged_in": bool(_g(r, "is_plugged_in", default=False)),
        "is_charging": bool(_g(r, "is_charging", default=False)),
        "charging_status": _CHARGING.get(int(_g(r, "charging_status", default=0)), "?"),
        "charging_type": _CTYPE.get(int(_g(r, "charging_type", default=0)), "?"),
        "connection": _CONN.get(int(_g(r, "charger_connection_status", default=0)), "?"),
    })
    await grab("amp_limit", "get_amp_limit", lambda r: _g(r, "amperage_limit"))
    await grab("availability", "get_availability", lambda r: {
        "is_available": bool(_g(r, "is_available", default=False)),
        "status": _AVAIL.get(int(_g(r, "availability_status", default=0)), "?"),
    })
    await grab("odometer_km", "get_odometer", lambda r: _g(r, "odometer_meters") and round(_g(r, "odometer_meters") / 1000))
    return out


async def get_c3_snapshot(force: bool = False) -> dict:
    """Return a decoded snapshot of the new C3 data. Cached for `_TTL_S`.

    Shape: {"ok": bool, "error": str|None, "age_s": int, ...sections}.
    Never raises — failures land in the returned dict.
    """
    global _cache, _cache_at
    now = time.monotonic()
    if not force and _cache is not None and (now - _cache_at) < _TTL_S:
        c = dict(_cache)
        c["age_s"] = round(now - _cache_at)
        return c

    async with _lock:
        now = time.monotonic()
        if not force and _cache is not None and (now - _cache_at) < _TTL_S:
            c = dict(_cache)
            c["age_s"] = round(now - _cache_at)
            return c
        try:
            await _ensure_client()
            data = await _collect()
            data["ok"] = True
            data["error"] = None
        except Exception:
            await _reset()
            # one clean retry (token may have expired between page views)
            try:
                await _ensure_client()
                data = await _collect()
                data["ok"] = True
                data["error"] = None
            except Exception as e2:
                await _reset()
                data = {"ok": False, "error": f"{type(e2).__name__}: {e2}"[:200]}
        data["age_s"] = 0
        _cache, _cache_at = data, time.monotonic()
        return dict(data)
