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
import html as _html
import re
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
_USAGE_MODE = {0: "—", 1: "abandoned", 2: "inactive", 3: "convenience",
               4: "active", 5: "driving", 6: "engine on", 7: "engine off"}
_UNAVAIL_REASON = {0: "—", 1: "no internet", 2: "power saving", 3: "car in use",
                   4: "OTA in progress", 5: "stolen tracking", 6: "service mode"}
_TCU_STATUS = {0: "not used", 1: "unavailable", 2: "disconnected", 3: "connected"}
_TCU_NETWORK = {0: "—", 1: "unknown", 2: "CDMA 1X", 3: "CDMA EVDO",
                4: "WCDMA", 5: "GSM", 6: "LTE"}
_TCU_SIGNAL = {0: "—", 1: "unknown", 2: "poor", 3: "good", 4: "strong"}
_SERVICE = {0: "—", 1: "no warning", 2: "unknown", 3: "maintenance soon",
            4: "engine-hours soon", 5: "distance soon", 6: "maintenance due",
            7: "engine-hours due", 8: "distance due"}
_SWSTATE = {0: "unknown", 1: "download ready", 2: "downloading",
            3: "download complete", 4: "download failed", 5: "install initiated",
            6: "installing", 7: "install aborted", 8: "install failed",
            9: "installed", 10: "install deferred", 11: "install failed (critical)",
            12: "install scheduled", 13: "install starting", 14: "unknown"}
# Software states that mean "a newer version is pending / available" (i.e. the
# car has a `new_sw_version` it hasn't finished installing). Excludes 0/14
# (unknown) and 9 (already installed) so we don't nag once it's done.
SW_PENDING_STATES = {1, 2, 3, 5, 6, 7, 10, 11, 12, 13}
# Per-bulb light-warning fields on Health, with human labels — for granular
# "which light is out" instead of the single any_light_failure flag.
_LIGHT_FIELDS = {
    "brake_light_left_warning": "brake L",
    "brake_light_center_warning": "brake C",
    "brake_light_right_warning": "brake R",
    "low_beam_left_warning": "low beam L",
    "low_beam_right_warning": "low beam R",
    "high_beam_left_warning": "high beam L",
    "high_beam_right_warning": "high beam R",
    "daytime_running_light_left_warning": "DRL L",
    "daytime_running_light_right_warning": "DRL R",
    "turn_indication_front_left_warning": "indicator FL",
    "turn_indication_front_right_warning": "indicator FR",
    "turn_indication_rear_left_warning": "indicator RL",
    "turn_indication_rear_right_warning": "indicator RR",
    "fog_light_front_warning": "fog front",
    "fog_light_rear_warning": "fog rear",
    "position_light_front_left_warning": "position FL",
    "position_light_front_right_warning": "position FR",
    "position_light_rear_left_warning": "position RL",
    "position_light_rear_right_warning": "position RR",
    "registration_plate_light_warning": "plate",
    "side_mark_lights_warning": "side marker",
}


def _clean_sw_notes(raw: Any) -> str | None:
    """The car's changelog comes as pseudo-HTML (<textblock>, <listelement>,
    <linefeed/>). Turn it into readable bulleted text."""
    if not raw:
        return None
    txt = str(raw)
    txt = re.sub(r"<listelement>", "\n• ", txt, flags=re.I)
    txt = re.sub(r"<linefeed\s*/?>", "\n", txt, flags=re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = _html.unescape(txt)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in txt.split("\n")]
    return "\n".join(ln for ln in lines if ln and ln != "•") or None


def _failed_lights(r: Any) -> list[str]:
    """Names of individual bulbs reporting a fault (warning code 2). Codes:
    1 = ok, 0/2+ = not-ok; we only flag a concrete fault (==2)."""
    out = []
    for field, label in _LIGHT_FIELDS.items():
        if int(_g(r, field, default=0)) == 2:
            out.append(label)
    return out

_TTL_S = 30.0  # cache window — gentle on the C3 rate limiter
_LIVE_LOCK_TIMEOUT_S = 6.0  # a non-forced (dashboard) read waits at most this
#   long for an in-flight poll fetch before degrading to cache/DB — both hit the
#   shared rate-limited account, so it must never queue behind a slow _collect.
_lock = asyncio.Lock()
_api: Any = None
_vehicle: Any = None
_cache: dict | None = None
_cache_at: float = 0.0
_climate_patched = False

# HeatingIntensity as returned by the car (matches polestar_api's own enum:
# UNSPECIFIED=0, OFF=1, LEVEL1..3=2..4).
_HEAT = {0: "—", 1: "off", 2: "level 1", 3: "level 2", 4: "level 3"}


def _patch_climate_digital_twin() -> None:
    """Widen the C3 climate decode with cabin/seat-heating fields the library
    parses internally but doesn't expose.

    `ClimateServiceClient._parse_digital_twin` already pulls raw protobuf
    fields 6-8 (ventilation flag, current temp, target temp) out of the
    digital-twin payload to *infer* a heat/cool action, then throws the
    numbers away. Fields 9-13 (seat heating FL/FR/RL/RR + steering wheel,
    same field order as `Vehicle.start_climate`'s parameters — see
    `c3_control.py`) aren't touched by this library version (0.3.0) at all;
    a community fork decodes them the same way. We re-decode the same bytes
    ourselves and attach the extras to the frozen `ClimatizationInfo` via
    `object.__setattr__`, mirroring the gRPC-battery patch in `client.py`."""
    global _climate_patched
    if _climate_patched:
        return
    try:
        from polestar_api.codec import decode
        from polestar_api.services.climate import ClimateServiceClient
    except Exception:
        return

    _orig = ClimateServiceClient._parse_digital_twin.__func__

    def _parse_digital_twin_with_extras(cls, climate_bytes: bytes):
        info = _orig(cls, climate_bytes)
        raw = decode(climate_bytes)
        for name, field in (
            ("current_temp_c", 7), ("target_temp_c", 8),
            ("seat_fl", 9), ("seat_fr", 10),
            ("seat_rl", 11), ("seat_rr", 12), ("steering_wheel", 13),
        ):
            object.__setattr__(info, name, raw.get(field))
        return info

    ClimateServiceClient._parse_digital_twin = classmethod(_parse_digital_twin_with_extras)
    _climate_patched = True


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


def _last_known_vin() -> str | None:
    """VIN from the most recent successful C3 poll — used only as a fallback
    when the vehicle-listing call itself is broken (see below); the DB always
    has one once C3 has synced successfully at least once."""
    from sqlmodel import Session, select

    from ..db.models import C3Snapshot
    from ..db.session import get_engine

    with Session(get_engine()) as s:
        return s.exec(
            select(C3Snapshot.vin).order_by(C3Snapshot.fetched_at.desc()).limit(1)
        ).first()


async def _ensure_client():
    global _api, _vehicle
    if _api is not None and _vehicle is not None:
        return
    from polestar_api import PolestarApi, Vehicle  # lazy
    from polestar_api.exceptions import ApiError

    _patch_climate_digital_twin()
    s = get_settings()
    api = PolestarApi(email=s.polestar_email, password=s.polestar_password)
    await api.async_init()
    try:
        vehicles = await api.get_vehicles()
        if not vehicles:
            raise RuntimeError("C3 returned no vehicles")
        vehicle = vehicles[0]
    except ApiError:
        # get_vehicles() runs a separate app-backend GraphQL query
        # (GetVDMSCars/VehicleInformation) that Polestar can break independently
        # of the gRPC/C3 connection async_init() just established above — e.g.
        # 2026-08-31, Polestar removed fields from VehicleInformation
        # server-side. Editing our copy of the query text has no effect: the
        # backend runs its own persisted/trusted copy of the operation
        # regardless of what we send (confirmed by testing; also reported
        # independently in kildahldev/unofficial-polestar-api#30). Skip the
        # broken listing call and build the Vehicle straight from a VIN we
        # already know plus the working connection instead — same workaround
        # the HA integration maintainer shipped for the same break. Loses
        # internal_id/registration_no/model_year/model_name (cosmetic only:
        # the Lab page's vehicle line); everything gRPC-backed (battery,
        # climate, locks, GPS, control) is unaffected.
        vin = _last_known_vin()
        if not vin:
            await _safe_close(api)
            raise
        vehicle = Vehicle(vin=vin, connection=api._connection)
    _api, _vehicle = api, vehicle


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
        # Net-new (community-decoded digital-twin fields, see
        # _patch_climate_digital_twin): only present while climate is active.
        "current_temp_c": _g(r, "current_temp_c"),
        "target_temp_c": _g(r, "target_temp_c"),
        "seat_fl": _HEAT.get(_g(r, "seat_fl", default=0), "?"),
        "seat_fr": _HEAT.get(_g(r, "seat_fr", default=0), "?"),
        "seat_rl": _HEAT.get(_g(r, "seat_rl", default=0), "?"),
        "seat_rr": _HEAT.get(_g(r, "seat_rr", default=0), "?"),
        "steering_wheel": _HEAT.get(_g(r, "steering_wheel", default=0), "?"),
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
        "engine_hours_to_service": _g(r, "engine_hours_to_service"),
        "brake_fluid": _WARN.get(int(_g(r, "brake_fluid_level_warning", default=0)), "?"),
        "oil": _WARN.get(int(_g(r, "oil_level_warning", default=0)), "?"),
        "coolant": _WARN.get(int(_g(r, "engine_coolant_level_warning", default=0)), "?"),
        "washer_fluid": _WARN.get(int(_g(r, "washer_fluid_level_warning", default=0)), "?"),
        "low_voltage_battery": _WARN.get(int(_g(r, "low_voltage_battery_warning", default=0)), "?"),
        "failed_lights": _failed_lights(r),
        "tyre_fl_kpa": _g(r, "front_left_tyre_pressure_kpa") or None,
        "tyre_fr_kpa": _g(r, "front_right_tyre_pressure_kpa") or None,
        "tyre_rl_kpa": _g(r, "rear_left_tyre_pressure_kpa") or None,
        "tyre_rr_kpa": _g(r, "rear_right_tyre_pressure_kpa") or None,
    })
    await grab("battery", "get_battery", lambda r: {
        "charge_level": _g(r, "charge_level"),
        "range_km": _g(r, "range_km"),
        "is_plugged_in": bool(_g(r, "is_plugged_in", default=False)),
        "is_charging": bool(_g(r, "is_charging", default=False)),
        "charging_status": _CHARGING.get(int(_g(r, "charging_status", default=0)), "?"),
        "charging_type": _CTYPE.get(int(_g(r, "charging_type", default=0)), "?"),
        "connection": _CONN.get(int(_g(r, "charger_connection_status", default=0)), "?"),
        # Net-new from C3: the car's own efficiency + live electricals (the
        # latter only populate while charging) + three charge-time horizons.
        "avg_consumption": _g(r, "avg_consumption"),
        "avg_consumption_since_charge": _g(r, "avg_consumption_since_charge"),
        "power_w": _g(r, "power_watts"),
        "current_a": _g(r, "current_amps"),
        "voltage_v": _g(r, "voltage_volts"),
        "time_to_full": _g(r, "time_to_full"),
        "time_to_target": _g(r, "time_to_target"),
        "time_to_min_soc": _g(r, "time_to_min_soc"),
        "total_consumption_wh_since_charge": _g(r, "total_consumption_wh_since_charge"),
    })
    await grab("software", "get_software_info", lambda r: {
        "new_version": _g(r, "new_sw_version"),
        "state_code": int(_g(r, "state", default=0)),
        "state": _SWSTATE.get(int(_g(r, "state", default=0)), "?"),
        "name": _g(r, "description", "name"),
        "notes": _clean_sw_notes(
            _g(r, "description", "long_desc") or _g(r, "description", "short_desc")
        ),
    })
    await grab("amp_limit", "get_amp_limit", lambda r: _g(r, "amperage_limit"))
    await grab("availability", "get_availability", lambda r: {
        "is_available": bool(_g(r, "is_available", default=False)),
        "status": _AVAIL.get(int(_g(r, "availability_status", default=0)), "?"),
        "usage_mode": _USAGE_MODE.get(int(_g(r, "usage_mode", default=0)), "?"),
        "unavailable_reason": _UNAVAIL_REASON.get(int(_g(r, "unavailable_reason", default=0)), "?"),
    })
    await grab("connectivity", "get_connectivity", lambda r: {
        "status": _TCU_STATUS.get(int(_g(r, "status", default=0)), "?"),
        "network": _TCU_NETWORK.get(int(_g(r, "network_type", default=0)), "?"),
        "signal": _TCU_SIGNAL.get(int(_g(r, "signal_strength", default=0)), "?"),
    })
    # Odometer + the car's own trip meters, from one get_odometer call. Keep
    # `odometer_km` as a scalar (c3_dict_to_row persists it); trips are net-new.
    fn = getattr(car, "get_odometer", None)
    if fn is not None:
        try:
            r = await asyncio.wait_for(fn(), timeout=25)
            om = _g(r, "odometer_meters")
            out["odometer_km"] = round(om / 1000) if om else None
            out["trips"] = {
                "auto_km": _g(r, "trip_meter_automatic_km"),
                "manual_km": _g(r, "trip_meter_manual_km"),
            }
        except Exception as e:
            out.setdefault("_errors", {})["odometer"] = f"{type(e).__name__}: {e}"[:160]
    return out


def _aged_cache(now: float) -> dict:
    """A copy of the live cache stamped with its current age."""
    c = dict(_cache)  # type: ignore[arg-type]
    c["age_s"] = round(now - _cache_at)
    return c


# Substrings that mark a rate-limit / auth failure of the shared account — a
# throttle ("not authorized") or a login that lost its relation to the car
# (PERMISSION_DENIED / re-pair). For these an immediate retry only hits the same
# limit again, so we skip it.
_THROTTLE_AUTH_MARKERS = (
    "not authorized", "unauthorized", "unauthenticated", "permission_denied",
    "permission denied", "429", "too many requests", "rate", "throttl",
)


def _is_throttle_or_auth(exc: Exception) -> bool:
    msg = f"{type(exc).__name__}: {exc}".lower()
    return any(m in msg for m in _THROTTLE_AUTH_MARKERS)


async def get_c3_snapshot(force: bool = False) -> dict:
    """Return a decoded snapshot of the new C3 data. Cached for `_TTL_S`.

    Shape: {"ok": bool, "error": str|None, "age_s": int, ...sections}.
    Never raises — failures land in the returned dict.
    """
    global _cache, _cache_at
    now = time.monotonic()
    if not force and _cache is not None and (now - _cache_at) < _TTL_S:
        return _aged_cache(now)

    # The poll loop (force=True) is the writer and always waits for the lock.
    # A dashboard read (force=False) must not block on an in-flight poll fetch —
    # both hit the shared rate-limited account — so it waits only briefly, then
    # degrades to the last cache (or a 'busy' marker → the route's DB fallback).
    if force:
        await _lock.acquire()
    else:
        try:
            await asyncio.wait_for(_lock.acquire(), timeout=_LIVE_LOCK_TIMEOUT_S)
        except TimeoutError:
            now = time.monotonic()
            return _aged_cache(now) if _cache is not None else {
                "ok": False, "error": "busy", "age_s": 0,
            }
    try:
        now = time.monotonic()
        if not force and _cache is not None and (now - _cache_at) < _TTL_S:
            return _aged_cache(now)
        try:
            await _ensure_client()
            data = await _collect()
            data["ok"] = True
            data["error"] = None
        except Exception as e1:
            await _reset()
            # A throttle / auth-denied first failure means the shared account is
            # rate-limited or needs re-pairing — retrying immediately just double-
            # taps the same limit, so don't. Only retry the transient case (e.g. a
            # token that expired between page views).
            if _is_throttle_or_auth(e1):
                data = {"ok": False, "error": f"{type(e1).__name__}: {e1}"[:200]}
            else:
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
    finally:
        _lock.release()


def c3_dict_to_row(data: dict) -> dict | None:
    """Project a decoded C3 snapshot (the `get_c3_snapshot` shape) onto the
    flat C3Snapshot columns. Returns None when the fetch failed (no point
    persisting an error row — those stay in the live cache only).

    Mirrors scheduler._persist: tolerant of the dict missing sections.
    """
    if not data.get("ok"):
        return None
    veh = data.get("vehicle") or {}
    loc = data.get("location") or {}
    parked = data.get("parked") or {}
    wx = data.get("weather") or {}
    ext = data.get("exterior") or {}
    doors = ext.get("doors") or {}
    wins = ext.get("windows") or {}
    cl = data.get("climate") or {}
    air = data.get("air") or {}
    h = data.get("health") or {}
    bat = data.get("battery") or {}
    avail = data.get("availability") or {}
    conn = data.get("connectivity") or {}

    def _door(name: str) -> str | None:
        d = doors.get(name)
        return d.get("open") if isinstance(d, dict) else None

    return {
        "vin": veh.get("vin") or "",
        "lat": loc.get("lat"),
        "lng": loc.get("lng"),
        "heading": loc.get("heading"),
        "speed": loc.get("speed"),
        "gps_ts": loc.get("ts"),
        "parked_lat": parked.get("lat"),
        "parked_lng": parked.get("lng"),
        "parked_ts": parked.get("ts"),
        "weather_temp_c": wx.get("temp_c"),
        "weather_ts": wx.get("ts_ms"),
        "is_locked": ext.get("is_locked"),
        "any_door_open": ext.get("any_door_open"),
        "central_lock": ext.get("central_lock"),
        "door_fl": _door("front_left"),
        "door_fr": _door("front_right"),
        "door_rl": _door("rear_left"),
        "door_rr": _door("rear_right"),
        "win_fl": wins.get("front_left"),
        "win_fr": wins.get("front_right"),
        "win_rl": wins.get("rear_left"),
        "win_rr": wins.get("rear_right"),
        "sunroof": ext.get("sunroof"),
        "hood": ext.get("hood"),
        "tailgate": ext.get("tailgate"),
        "tank_lid": ext.get("tank_lid"),
        "climate_active": cl.get("is_active"),
        "climate_action": cl.get("action"),
        "climate_running": cl.get("running"),
        "climate_time_remaining": cl.get("time_remaining"),
        "climate_current_temp_c": cl.get("current_temp_c"),
        "climate_target_temp_c": cl.get("target_temp_c"),
        "seat_heat_fl": cl.get("seat_fl"),
        "seat_heat_fr": cl.get("seat_fr"),
        "seat_heat_rl": cl.get("seat_rl"),
        "seat_heat_rr": cl.get("seat_rr"),
        "steering_wheel_heat": cl.get("steering_wheel"),
        "air_running": air.get("is_running"),
        "air_aqi": air.get("aqi"),
        "air_pm25": air.get("pm25"),
        "any_light_failure": h.get("any_light_failure"),
        "any_tyre_warning": h.get("any_tyre_warning"),
        "service_warning": h.get("service_warning"),
        "days_to_service": h.get("days_to_service"),
        "distance_to_service_km": h.get("distance_to_service_km"),
        "brake_fluid": h.get("brake_fluid"),
        "oil": h.get("oil"),
        "coolant": h.get("coolant"),
        "washer_fluid": h.get("washer_fluid"),
        "low_voltage_battery": h.get("low_voltage_battery"),
        "engine_hours_to_service": h.get("engine_hours_to_service"),
        "failed_lights": ",".join(h.get("failed_lights") or []) or None,
        "charge_level": bat.get("charge_level"),
        "range_km": bat.get("range_km"),
        "is_plugged_in": bat.get("is_plugged_in"),
        "is_charging": bat.get("is_charging"),
        "charging_status": bat.get("charging_status"),
        "charging_type": bat.get("charging_type"),
        "connection": bat.get("connection"),
        # 0 means "no estimate" (idle/done) — store NULL so charts/queries can
        # filter on IS NOT NULL instead of guessing what 0 means.
        "time_to_full": bat.get("time_to_full") or None,
        "time_to_target": bat.get("time_to_target") or None,
        "time_to_min_soc": bat.get("time_to_min_soc") or None,
        "amp_limit": data.get("amp_limit"),
        "odometer_km": data.get("odometer_km"),
        "availability_status": avail.get("status"),
        "usage_mode": avail.get("usage_mode"),
        "unavailable_reason": avail.get("unavailable_reason"),
        "tyre_fl_kpa": h.get("tyre_fl_kpa"),
        "tyre_fr_kpa": h.get("tyre_fr_kpa"),
        "tyre_rl_kpa": h.get("tyre_rl_kpa"),
        "tyre_rr_kpa": h.get("tyre_rr_kpa"),
        "tcu_status": conn.get("status"),
        "tcu_network": conn.get("network"),
        "tcu_signal": conn.get("signal"),
    }
