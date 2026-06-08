"""Notifications driven by the C3 backend (doors, locks, openings, health).

Fires on a *transition* into the alerting state (closed → open, locked →
unlocked) so a parked car that simply stays open doesn't re-alert every poll;
``notify()``'s NotificationLog then dedups within the day as a second guard.

Conservative by design — like ``anomalies.py``, a false "door open" erodes
trust fast. We only consider the car "parked" (speed ~0) before alerting on an
opening, so a window cracked while driving doesn't nag.
"""

from __future__ import annotations

import asyncio
import logging

from .app_settings import alert_enabled, in_alert_window
from .db.models import C3Snapshot
from .i18n import t
from .notify import notify

log = logging.getLogger(__name__)

# Openings we watch, mapped to their human label key.
_OPENINGS: list[tuple[str, str]] = [
    ("door_fl", "front-left door"),
    ("door_fr", "front-right door"),
    ("door_rl", "rear-left door"),
    ("door_rr", "rear-right door"),
    ("win_fl", "front-left window"),
    ("win_fr", "front-right window"),
    ("win_rl", "rear-left window"),
    ("win_rr", "rear-right window"),
    ("sunroof", "sunroof"),
    ("hood", "frunk"),
    ("tailgate", "boot"),
]

_OPEN_STATES = {"open", "ajar"}

# Level / fluid warnings decoded as _WARN strings on the C3 snapshot. A "bad"
# reading is anything past ok; the 12V battery is the one we most want to catch
# early. Alerted on transition (ok → bad), deduped per day by notify().
_WARN_BAD = {"warning", "too low", "too high"}
_LEVEL_WARNINGS: list[tuple[str, str]] = [
    ("low_voltage_battery", "12V battery"),
    ("washer_fluid", "washer fluid"),
    ("brake_fluid", "brake fluid"),
    ("oil", "oil level"),
    ("coolant", "coolant level"),
]


def _is_bad(value) -> bool:  # noqa: ANN001
    return value in _WARN_BAD

def _local_hour(row: C3Snapshot) -> int:
    """The snapshot's local (Europe/Vienna) hour. Quiet-hour windows for the
    "left open / unlocked" nags are configured per-alert in settings — only
    alert overnight by default, when the car is parked and the owner can act."""
    try:
        from zoneinfo import ZoneInfo

        ts = row.fetched_at
        # Only stamp UTC when naive; otherwise we'd clobber the caller's tz.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ZoneInfo("UTC"))
        local = ts.astimezone(ZoneInfo("Europe/Vienna"))
    except Exception:  # noqa: BLE001
        local = row.fetched_at
    return local.hour


def _is_parked(row: C3Snapshot) -> bool:
    """True when the car is (almost certainly) stationary. Treat unknown speed
    as parked — the alert only matters when the car is left, and a missing
    speed reading is the common case for a sleeping car."""
    return row.speed is None or row.speed < 1


def _day_key(row: C3Snapshot, part: str) -> str:
    return f"{row.vin}-{row.fetched_at:%Y-%m-%d}-{part}"


def _opened(row: C3Snapshot, prev: C3Snapshot | None, attr: str) -> bool:
    """The opening just transitioned into an open/ajar state."""
    now = getattr(row, attr)
    if now not in _OPEN_STATES:
        return False
    if prev is None:
        return True  # first sighting while open — worth one alert
    return getattr(prev, attr) not in _OPEN_STATES


def _check_openings(row: C3Snapshot, prev: C3Snapshot | None) -> None:
    if not alert_enabled("c3_open"):
        return
    if not in_alert_window("c3_open", _local_hour(row)):
        return  # door/window-open nags only inside the configured window
    if not _is_parked(row):
        return
    for attr, label in _OPENINGS:
        if not _opened(row, prev, attr):
            continue
        notify(
            kind="c3_open",
            key=_day_key(row, attr),
            title=t("{part} left open", part=t(label)),
            body=t("The {part} is open while the car is parked.", part=t(label)),
            vin=row.vin,
            priority="high",
            tags=["warning", "car"],
        )


def _check_unlocked(row: C3Snapshot, prev: C3Snapshot | None) -> None:
    if not alert_enabled("c3_unlocked"):
        return
    if not in_alert_window("c3_unlocked", _local_hour(row)):
        return  # unlocked-while-parked nag only inside the configured window
    if row.is_locked is not False:  # None (unknown) or True → no alert
        return
    if not _is_parked(row):
        return
    # Transition locked → unlocked (or first sighting unlocked while parked).
    if prev is not None and prev.is_locked is False:
        return
    notify(
        kind="c3_unlocked",
        key=_day_key(row, "unlocked"),
        title=t("Car left unlocked"),
        body=t("The car is parked and unlocked."),
        vin=row.vin,
        priority="high",
        tags=["warning", "lock"],
    )


def _check_health(row: C3Snapshot, prev: C3Snapshot | None) -> None:
    if (
        alert_enabled("c3_tyre")
        and row.any_tyre_warning
        and not (prev and prev.any_tyre_warning)
    ):
        notify(
            kind="c3_tyre",
            key=_day_key(row, "tyre"),
            title=t("Tyre warning"),
            body=t("The car reports a tyre pressure warning."),
            vin=row.vin,
            tags=["warning"],
        )
    if (
        alert_enabled("c3_light")
        and row.any_light_failure
        and not (prev and prev.any_light_failure)
    ):
        body = t("The car reports a bulb/light failure.")
        if row.failed_lights:
            body = t("The car reports a light failure: {bulbs}.", bulbs=row.failed_lights)
        notify(
            kind="c3_light",
            key=_day_key(row, "light"),
            title=t("Light failure"),
            body=body,
            vin=row.vin,
            tags=["warning"],
        )

    # Level / fluid warnings — 12V battery especially. Transition ok → bad.
    if not alert_enabled("c3_level"):
        return
    for attr, label in _LEVEL_WARNINGS:
        now = getattr(row, attr, None)
        if not _is_bad(now):
            continue
        if prev is not None and _is_bad(getattr(prev, attr, None)):
            continue  # already alerted on the transition into the bad state
        notify(
            kind="c3_level",
            key=_day_key(row, attr),
            title=t("{part} warning", part=t(label)),
            body=t("The car reports a {part} warning ({state}).",
                   part=t(label), state=now),
            vin=row.vin,
            priority="high",
            tags=["warning"],
        )


def _windows_open(row: C3Snapshot) -> list[str]:
    """Labels of any open windows / sunroof (the openings rain can get through)."""
    parts = {
        "win_fl": "front-left window", "win_fr": "front-right window",
        "win_rl": "rear-left window", "win_rr": "rear-right window",
        "sunroof": "sunroof",
    }
    return [label for attr, label in parts.items() if getattr(row, attr) in _OPEN_STATES]


async def check_rain_windows(row: C3Snapshot) -> None:
    """Alert once/day if it's raining at the car's location while a window or
    the sunroof is open. The weather lookup only runs when a window is actually
    open (rare), so it costs nothing on the common all-closed poll."""
    if not alert_enabled("c3_rain_windows"):
        return
    if row.lat is None or row.lng is None:
        return
    open_labels = _windows_open(row)
    if not open_labels:
        return
    from .weather import current_at

    wx = await current_at(row.lat, row.lng)
    if not wx or not wx.get("is_rain"):
        return
    parts = ", ".join(t(lbl) for lbl in open_labels)
    # notify() does blocking ntfy/SMTP/Telegram I/O; this is the one C3 check
    # awaited directly on the event loop, so offload the send to a worker.
    await asyncio.to_thread(
        notify,
        kind="c3_rain_windows",
        key=_day_key(row, "rainwindows"),
        title=t("Rain & windows open"),
        body=t("It's raining at the car and these are open: {parts}.", parts=parts),
        vin=row.vin,
        priority="high",
        tags=["warning", "rain"],
    )


def run_c3_anomaly_checks(row: C3Snapshot, *, prev: C3Snapshot | None = None) -> None:
    # Each check self-gates on its per-alert config (enable + quiet-hour window),
    # so callers just run them all. The legacy NOTIFY_DOOR_OPEN/NOTIFY_UNLOCKED
    # env flags now seed the default-enabled state (see app_settings).
    _check_openings(row, prev)
    _check_unlocked(row, prev)
    _check_health(row, prev)
