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
from datetime import datetime, timedelta

from sqlalchemy import or_
from sqlmodel import Session, select

from .app_settings import alert_enabled, in_alert_window
from .config import get_settings
from .db.models import C3Snapshot
from .db.session import get_engine
from .i18n import t
from .notify import notify
from .util import fmt_local, to_local

log = logging.getLogger(__name__)

# State fields that prove the C3 backend actually returned vehicle data. When the
# C3 login degrades (lost car relation / throttle → every section
# PERMISSION_DENIED) the poll still stores a row, but with all of these `None`.
_STATE_FIELDS = (
    "is_locked", "door_fl", "door_fr", "door_rl", "door_rr",
    "sunroof", "hood", "tailgate", "tank_lid", "lat",
)
def disconnect_gap() -> timedelta:
    """How stale the last good fix must be before a gap counts as a real C3
    outage (vs a normal cadence pause). Derived from the *slowest* configured
    cadence — the night interval — so a 30-min night poll never reads as an
    outage and a single missed night poll doesn't false-alarm: 1.5× the night
    interval, floored at 30 min. Time-based (not poll-count) so it's correct
    across cadence changes *and* across an app restart mid-outage — the old
    12-row lookback went blind the moment the whole window was stateless (e.g. a
    restart 4 h into an outage). Shared by the disconnect alert and the trail /
    coverage gap so both agree on what "a gap" means."""
    night = get_settings().c3_night_poll_interval_min
    return timedelta(minutes=max(30, round(night * 1.5)))

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
    return to_local(row.fetched_at).hour


def _is_parked(row: C3Snapshot) -> bool:
    """True when the car is (almost certainly) stationary. Treat unknown speed
    as parked — the alert only matters when the car is left, and a missing
    speed reading is the common case for a sleeping car."""
    return row.speed is None or row.speed < 1


def _day_key(row: C3Snapshot, part: str) -> str:
    return f"{row.vin}-{fmt_local(row.fetched_at, '%Y-%m-%d')}-{part}"


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


def _check_climate(row: C3Snapshot, prev: C3Snapshot | None) -> None:
    """Confirm climate/preconditioning start and stop with a push notification —
    the official Polestar app gives no delivery feedback for a remote-start
    command, so this is the only confirmation the owner gets that the car
    actually reacted (regardless of which app sent the command). Fires once per
    transition; ``notify()``'s dedup then guards repeats within the same minute."""
    if not alert_enabled("c3_climate"):
        return
    now_active = bool(row.climate_active)
    was_active = bool(prev.climate_active) if prev is not None else False
    if now_active == was_active:
        return
    key = f"{row.vin}-{row.fetched_at:%Y%m%d-%H%M%S}"
    if now_active:
        notify(
            kind="c3_climate",
            key=key,
            title=t("Climate started"),
            body=t("Preconditioning is running ({action}).", action=t(row.climate_action or "climate")),
            vin=row.vin,
            tags=["car"],
        )
    else:
        notify(
            kind="c3_climate",
            key=key,
            title=t("Climate stopped"),
            body=t("Preconditioning has finished or was stopped."),
            vin=row.vin,
            tags=["car"],
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


def _c3_has_state(row: C3Snapshot | None) -> bool:
    """True when this poll actually carried vehicle state (not an all-`None`
    PERMISSION_DENIED row left behind by a degraded C3 login)."""
    if row is None:
        return False
    return any(getattr(row, f, None) is not None for f in _STATE_FIELDS)


def _last_stateful_at(vin: str, *, before: datetime | None = None) -> datetime | None:
    """Timestamp of the newest C3 poll that actually carried vehicle state, for
    this VIN, across the **whole** table (optionally strictly before a cutoff).
    Whole-table — not a fixed recent-row window — so detection survives an app
    restart and reports the true outage onset even after a day-long gap. The
    ``or_(state field not NULL)`` filter also skips any legacy all-`None` rows
    left behind before we stopped persisting stateless polls."""
    has_state = or_(*[getattr(C3Snapshot, f).is_not(None) for f in _STATE_FIELDS])
    with Session(get_engine()) as s:
        stmt = (
            select(C3Snapshot.fetched_at)
            .where(C3Snapshot.vin == vin)
            .where(has_state)
            .order_by(C3Snapshot.fetched_at.desc())
            .limit(1)
        )
        if before is not None:
            stmt = stmt.where(C3Snapshot.fetched_at < before)
        return s.exec(stmt).first()


def check_c3_disconnected(vin: str, now: datetime) -> None:
    """Alert when the C3 backend stops returning live data — the car account has
    lost its relation to the vehicle (a throttle that won't clear, or a login that
    needs **re-pairing**): every section comes back PERMISSION_DENIED, so the poll
    carries no usable state and we no longer persist a row for it. Called from the
    scheduler's outage branch (there is no fresh row to pass). Fires once per
    episode (keyed on the onset = the last good fix), only after the outage has
    persisted past a transient throttle, and only if the car *had* been returning
    data — so a never-paired car doesn't nag."""
    if not alert_enabled("c3_disconnected"):
        return
    last_good = _last_stateful_at(vin)
    if last_good is None:
        return  # never connected — not a live "drop"
    if now - last_good < disconnect_gap():
        return  # transient — wait for it to persist past a throttle blip
    notify(
        kind="c3_disconnected",
        # One alert per episode: the onset is stable for the whole outage.
        key=f"{vin}-{last_good:%Y%m%d-%H%M}",
        title=t("Vehicle connection lost"),
        body=t(
            "The car stopped returning live data around {time} — door/lock/GPS "
            "state is unavailable. The car account likely needs re-pairing in "
            "the Polestar app / settings.",
            time=fmt_local(last_good),
        ),
        vin=vin,
        priority="high",
        tags=["warning", "car"],
    )


def _last_online_at(vin: str, *, before: datetime | None = None) -> datetime | None:
    """Newest poll where the *car itself* reported a live connection
    (``unavailable_reason`` other than ``"no internet"``). Distinct from
    ``_last_stateful_at``: when the car's modem/TCU drops out, the C3 backend
    keeps serving the last cached state (state fields stay non-`None`, so
    ``_c3_has_state`` sees a normal poll) — only ``unavailable_reason`` shows
    it. Rows predating that field (`None`) count as online, not as a drop."""
    online = or_(
        C3Snapshot.unavailable_reason.is_(None),
        C3Snapshot.unavailable_reason != "no internet",
    )
    with Session(get_engine()) as s:
        stmt = (
            select(C3Snapshot.fetched_at)
            .where(C3Snapshot.vin == vin)
            .where(online)
            .order_by(C3Snapshot.fetched_at.desc())
            .limit(1)
        )
        if before is not None:
            stmt = stmt.where(C3Snapshot.fetched_at < before)
        return s.exec(stmt).first()


def _check_c3_no_internet(row: C3Snapshot) -> None:
    """Alert when the car reports ``unavailable_reason == "no internet"``
    (its own modem/SIM is down) for longer than ``disconnect_gap()``, and clear
    it on recovery. This is a second, independent outage mode from
    ``check_c3_disconnected``'s PERMISSION_DENIED case: here the C3 backend
    keeps returning a "successful" poll with the last cached door/lock/GPS
    state frozen in place, so the all-`None` detector never sees it — a
    multi-hour real-world outage went unnoticed and unnotified this way.
    Shares the ``c3_disconnected``/``c3_reconnected`` kind + toggle: from the
    owner's perspective it's the same 'car unreachable' event."""
    if not alert_enabled("c3_disconnected"):
        return
    if row.unavailable_reason == "no internet":
        last_online = _last_online_at(row.vin, before=row.fetched_at)
        if last_online is None:
            return  # never seen online — nothing to compare against
        if row.fetched_at - last_online < disconnect_gap():
            return  # transient — wait for it to persist past a blip
        notify(
            kind="c3_disconnected",
            key=f"{row.vin}-{last_online:%Y%m%d-%H%M}",
            title=t("Vehicle connection lost"),
            body=t(
                "The car has been reporting 'no internet' since around {time} "
                "— door/lock/GPS state is frozen and may be stale. Check the "
                "car's mobile connection.",
                time=fmt_local(last_online),
            ),
            vin=row.vin,
            priority="high",
            tags=["warning", "car"],
        )
        return
    prev_online = _last_online_at(row.vin, before=row.fetched_at)
    if prev_online is None:
        return  # first-ever poll — not a recovery
    if row.fetched_at - prev_online < disconnect_gap():
        return  # normal cadence gap — no confirmed outage just ended
    notify(
        kind="c3_reconnected",
        key=f"{row.vin}-{prev_online:%Y%m%d-%H%M}",
        title=t("Vehicle connection restored"),
        body=t("The car is returning live data again — door/lock/GPS state is "
               "back. The connection was lost around {time}.",
               time=fmt_local(prev_online)),
        vin=row.vin,
        tags=["car"],
    )


def _check_c3_reconnected(row: C3Snapshot) -> None:
    """All-clear when C3 starts returning live data again after a confirmed
    outage. Fires once, on the first connected poll whose gap to the previous good
    fix exceeded the disconnect threshold — so a transient blip gets no recovery
    ping either. The onset (= that previous good fix) is read whole-table, so the
    reported outage window is the real one, not a recent-window truncation. Shares
    the ``c3_disconnected`` toggle: one switch governs the connection-state pair."""
    if not alert_enabled("c3_disconnected"):
        return
    if not _c3_has_state(row):
        return  # still down — nothing to clear
    prev_good = _last_stateful_at(row.vin, before=row.fetched_at)
    if prev_good is None:
        return  # no prior good fix — first-ever connect, not a recovery
    if row.fetched_at - prev_good < disconnect_gap():
        return  # normal cadence gap — no confirmed outage just ended
    notify(
        kind="c3_reconnected",
        # Keyed on the ended outage's onset so the all-clear is sent exactly once,
        # matching the c3_disconnected key for the same episode.
        key=f"{row.vin}-{prev_good:%Y%m%d-%H%M}",
        title=t("Vehicle connection restored"),
        body=t("The car is returning live data again — door/lock/GPS state is "
               "back. The connection was lost around {time}.",
               time=fmt_local(prev_good)),
        vin=row.vin,
        tags=["car"],
    )


def run_c3_anomaly_checks(row: C3Snapshot, *, prev: C3Snapshot | None = None) -> None:
    # Each check self-gates on its per-alert config (enable + quiet-hour window),
    # so callers just run them all. The legacy NOTIFY_DOOR_OPEN/NOTIFY_UNLOCKED
    # env flags now seed the default-enabled state (see app_settings).
    # The disconnect alert fires from the scheduler's outage branch (no row to
    # pass there); here we only emit the reconnect all-clear on a good poll.
    _check_c3_reconnected(row)
    _check_c3_no_internet(row)
    _check_openings(row, prev)
    _check_unlocked(row, prev)
    _check_health(row, prev)
    _check_climate(row, prev)
