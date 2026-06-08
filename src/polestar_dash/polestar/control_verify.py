"""Verify a C3 write command actually changed the car's physical state.

`run_command` (c3_control.py) returns only the backend's *invocation* status —
SUCCESS means "the car accepted the command", not "the window is now closed".
This module maps each command to the post-state we expect on a fresh C3 read,
so the UI (and the automation engine) can confirm ✓ verified / ✗ not confirmed
instead of guessing.

Some commands have no C3 read-back: `set_target_soc` lives on the pypolestar
VehicleSnapshot (not C3), and flash/honk/wakeup are momentary. Those return
``verified=None`` ("sent — can't verify"), never a false ✗.

Reads the same flat row the dashboard uses (`c3.c3_dict_to_row`), so the field
names here are the C3Snapshot columns.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

_OPEN = {"open", "ajar"}
_UNKNOWN = {None, "—", "?"}
_WIN = ["win_fl", "win_fr", "win_rl", "win_rr"]


def _amp_check(row: dict, params: dict) -> bool:
    a = row.get("amp_limit")
    try:
        return a is not None and abs(float(a) - float(params["amperage"])) < 0.5
    except (KeyError, TypeError, ValueError):
        return False


# command -> {fields, check(row, params)->bool, expected(params)->str}.
# A command absent from this map is treated as not verifiable (verified=None).
_VERIFY: dict[str, dict[str, Any]] = {
    "lock": {"fields": ["is_locked"], "expected": "locked",
             "check": lambda r, p: r.get("is_locked") is True},
    "unlock": {"fields": ["is_locked"], "expected": "unlocked",
               "check": lambda r, p: r.get("is_locked") is False},
    "windows_close": {"fields": _WIN, "expected": "all closed",
                      "check": lambda r, p: all(r.get(f) == "closed" for f in _WIN)},
    "windows_open": {"fields": _WIN, "expected": "open",
                     "check": lambda r, p: all(r.get(f) in _OPEN for f in _WIN)},
    "climate_start": {"fields": ["climate_active"], "expected": "climate on",
                      "check": lambda r, p: r.get("climate_active") is True},
    "climate_stop": {"fields": ["climate_active"], "expected": "climate off",
                     "check": lambda r, p: r.get("climate_active") is False},
    "precleaning_start": {"fields": ["air_running"], "expected": "pre-cleaning on",
                          "check": lambda r, p: r.get("air_running") is True},
    "precleaning_stop": {"fields": ["air_running"], "expected": "pre-cleaning off",
                         "check": lambda r, p: r.get("air_running") is False},
    "charge_start": {"fields": ["is_charging"], "expected": "charging",
                     "check": lambda r, p: r.get("is_charging") is True},
    "charge_stop": {"fields": ["is_charging"], "expected": "not charging",
                    "check": lambda r, p: r.get("is_charging") is False},
    "unlock_trunk": {"fields": ["tailgate"], "expected": "boot open",
                     "check": lambda r, p: r.get("tailgate") in _OPEN},
    "set_amp_limit": {"fields": ["amp_limit"],
                      "expected": lambda p: f"{p.get('amperage')} A",
                      "check": _amp_check},
}


def is_verifiable(command: str) -> bool:
    return command in _VERIFY


def _expected_text(spec: dict, params: dict) -> str:
    exp = spec["expected"]
    return exp(params) if isinstance(exp, Callable) else str(exp)


def predicate(command: str, params: dict | None, flat_row: dict | None) -> dict:
    """Compare a fresh flat C3 row against the command's expected post-state.

    Returns {verified: bool|None, expected: str, actual: dict}. ``verified`` is
    None when the command isn't verifiable or the relevant fields are unknown
    (so the UI shows "sent — can't verify", not a false failure).
    """
    params = params or {}
    spec = _VERIFY.get(command)
    if spec is None:
        return {"verified": None, "expected": "", "actual": {}}
    fields = spec["fields"]
    actual = {f: (flat_row or {}).get(f) for f in fields}
    expected = _expected_text(spec, params)
    if flat_row is None or all(actual.get(f) in _UNKNOWN for f in fields):
        return {"verified": None, "expected": expected, "actual": actual}
    try:
        ok: bool | None = bool(spec["check"](flat_row, params))
    except Exception:  # noqa: BLE001 — a flaky field must never crash the check
        ok = None
    return {"verified": ok, "expected": expected, "actual": actual}


# Soft floor against a misbehaving client hammering forced backend reads (each
# forced read shares the poll's C3 rate-limit bucket — throttling shows up as
# "not authorized"). The UI already backs off 3/6/9 s; this is a second guard.
_MIN_INTERVAL_S = 2.0
_last_forced = 0.0


def _too_soon() -> bool:
    global _last_forced
    now = time.monotonic()
    if now - _last_forced < _MIN_INTERVAL_S:
        return True
    _last_forced = now
    return False


async def verify_command(command: str, params: dict | None = None) -> dict:
    """Force one fresh C3 read and return the predicate verdict for `command`.

    Never raises — a backend error lands as {ok: False, verified: None}. Used by
    the control UI's verify poll and by the automation engine after it acts.
    """
    if not is_verifiable(command):
        return {"ok": True, "command": command, "verified": None, "expected": "",
                "actual": {}}
    if _too_soon():
        return {"ok": True, "command": command, "verified": None, "busy": True}
    from .c3 import c3_dict_to_row, get_c3_snapshot

    data = await get_c3_snapshot(force=True)
    if not data.get("ok"):
        return {"ok": False, "command": command, "verified": None,
                "error": data.get("error")}
    flat = c3_dict_to_row(data) or {}
    verdict = predicate(command, params or {}, flat)
    verdict.update({"ok": True, "command": command, "age_s": data.get("age_s", 0)})
    return verdict
