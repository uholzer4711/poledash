"""Experimental C3 *control* — write commands to the car (lock, climate, …).

DELIBERATELY isolated, like its read-only sibling `c3.py`, and one step more
cautious because every call here has a real-world side effect on the car:

- It reuses `c3`'s shared client, async lock and reset helpers, so commands and
  the read snapshot never hit the C3 backend concurrently (same rate-limit
  bucket — throttling shows up as "not authorized", see lab research).
- Each command runs through one clean reset+retry, mirroring the read path
  (the OAuth token can expire between page views).
- Nothing is scheduled and nothing is persisted; this is driven only by the
  Test page. Gate the UI behind your own judgement — these move real hardware.

Verified working on Polestar 2 (MY2026): `start_climate` returns
InvocationStatus.SUCCESS. Note `wakeup()` is NOT implemented on this backend
(the car accepts commands without it).
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from polestar_api.models import (
    ChargeTargetLevelSettingType,
    HeatingIntensity,
    InvocationStatus,
)
from polestar_api.vehicle import HonkFlashAction

from . import c3

# wait a touch longer than reads — commands round-trip to the car
_CMD_TIMEOUT_S = 45.0

_INV_NAME = {int(s): s.name for s in InvocationStatus}


def _heat(level: Any) -> HeatingIntensity:
    """Coerce a 0–4 UI value into a HeatingIntensity (0/UNSPECIFIED = leave)."""
    try:
        return HeatingIntensity(int(level))
    except (ValueError, TypeError):
        return HeatingIntensity.UNSPECIFIED


# --- command builders: (vehicle, params) -> awaitable -------------------------
# Each entry is an async callable. Keep this an explicit allowlist — never
# dispatch to an arbitrary getattr() on the vehicle.
_COMMANDS: dict[str, Callable[[Any, dict], Any]] = {
    "lock": lambda car, p: car.lock(),
    "unlock": lambda car, p: car.unlock(),
    "unlock_trunk": lambda car, p: car.unlock_trunk(),
    "flash": lambda car, p: car.honk_flash(HonkFlashAction.FLASH),
    "honk_flash": lambda car, p: car.honk_flash(HonkFlashAction.HONK_AND_FLASH),
    "climate_start": lambda car, p: car.start_climate(
        temperature=float(p.get("temperature") or 0.0),
        front_left_seat=_heat(p.get("front_seat")),
        front_right_seat=_heat(p.get("front_seat")),
        rear_left_seat=_heat(p.get("rear_seat")),
        rear_right_seat=_heat(p.get("rear_seat")),
        steering_wheel=_heat(p.get("steering_wheel")),
    ),
    "climate_stop": lambda car, p: car.stop_climate(),
    "precleaning_start": lambda car, p: car.start_precleaning(),
    "precleaning_stop": lambda car, p: car.stop_precleaning(),
    "windows_open": lambda car, p: car.open_windows(),
    "windows_close": lambda car, p: car.close_windows(),
    "charge_start": lambda car, p: car.start_charging(),
    "charge_stop": lambda car, p: car.stop_charging(),
    "set_amp_limit": lambda car, p: car.set_amp_limit(amperage=int(p["amperage"])),
    "set_target_soc": lambda car, p: car.set_target_soc(
        level=int(p["level"]),
        setting_type=ChargeTargetLevelSettingType.DAILY,
    ),
    "wakeup": lambda car, p: car.wakeup(),
}

# Commands the UI exposes, with metadata for rendering. `confirm` flags the ones
# that move hardware in a way you'd want to think twice about.
COMMAND_META: list[dict] = [
    {"id": "climate_start", "group": "climate", "label": "Start climate", "confirm": False},
    {"id": "climate_stop", "group": "climate", "label": "Stop climate", "confirm": False},
    {"id": "precleaning_start", "group": "climate", "label": "Start air pre-cleaning", "confirm": False},
    {"id": "precleaning_stop", "group": "climate", "label": "Stop pre-cleaning", "confirm": False},
    {"id": "lock", "group": "security", "label": "Lock", "confirm": True},
    {"id": "unlock", "group": "security", "label": "Unlock", "confirm": True},
    {"id": "unlock_trunk", "group": "security", "label": "Unlock trunk", "confirm": True},
    {"id": "flash", "group": "security", "label": "Flash lights", "confirm": False},
    {"id": "honk_flash", "group": "security", "label": "Honk + flash", "confirm": True},
    {"id": "windows_open", "group": "windows", "label": "Open windows", "confirm": True},
    {"id": "windows_close", "group": "windows", "label": "Close windows", "confirm": True},
    {"id": "charge_start", "group": "charging", "label": "Start charging", "confirm": True},
    {"id": "charge_stop", "group": "charging", "label": "Stop charging", "confirm": True},
    {"id": "set_amp_limit", "group": "charging", "label": "Set amp limit", "confirm": True},
    {"id": "set_target_soc", "group": "charging", "label": "Set target SoC", "confirm": True},
    {"id": "wakeup", "group": "other", "label": "Wake up", "confirm": False},
]


def _decode(res: Any) -> dict:
    """Map a heterogeneous command response onto {ok, status, message, id}."""
    inv = getattr(res, "response", None)
    if inv is not None and hasattr(inv, "status"):
        st = inv.status
        return {
            "ok": getattr(st, "name", "") == "SUCCESS",
            "status": getattr(st, "name", str(st)),
            "message": getattr(inv, "message", "") or None,
            "id": getattr(inv, "id", None),
        }
    if isinstance(res, int):
        name = _INV_NAME.get(res, str(res))
        return {"ok": res == int(InvocationStatus.SUCCESS), "status": name,
                "message": None, "id": None}
    if res is None:  # e.g. start_precleaning returns None on success
        return {"ok": True, "status": "OK", "message": None, "id": None}
    return {"ok": True, "status": str(res)[:120], "message": None, "id": None}


async def run_command(command: str, params: dict | None = None) -> dict:
    """Fire one C3 command. Never raises — failures land in the returned dict.

    Shares `c3`'s client + lock so a command can't race the read snapshot.
    """
    params = params or {}
    builder = _COMMANDS.get(command)
    if builder is None:
        return {"ok": False, "command": command, "error": "unknown command"}

    async with c3._lock:
        try:
            await c3._ensure_client()
            res = await asyncio.wait_for(builder(c3._vehicle, params), timeout=_CMD_TIMEOUT_S)
        except Exception:
            await c3._reset()
            try:  # one clean retry — token may have expired
                await c3._ensure_client()
                res = await asyncio.wait_for(builder(c3._vehicle, params), timeout=_CMD_TIMEOUT_S)
            except Exception as e:
                await c3._reset()
                return {"ok": False, "command": command,
                        "error": f"{type(e).__name__}: {e}"[:240]}

    # the cached read snapshot is now stale — force a fresh one next refresh
    c3._cache = None
    out = _decode(res)
    out["command"] = command
    return out
