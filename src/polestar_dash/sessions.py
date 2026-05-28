"""Charging session detection from the snapshot stream."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlmodel import Session, select

from .app_settings import get_calibrated_battery_kwh, get_charging_loss_factor
from .db.models import ChargeSession, VehicleSnapshot
from .db.session import get_engine
from .polestar.fetcher import Snapshot

log = logging.getLogger(__name__)

# Rough Polestar 2 battery capacity in kWh. Used only for an *estimated*
# session energy when the API doesn't expose Wh directly. Long-range AWD/SR is
# 78/82 kWh; we pick a sensible middle value — the dashboard labels it as an
# estimate.
DEFAULT_BATTERY_KWH = 78.0

CHARGING_STATES = {"Charging", "Smart Charging"}
TERMINAL_STATES = {"Done", "Idle", "Fault", "Error"}

# Heuristic fallback for short / silent charges where the Polestar API never
# flips `charging_status` to "Charging" (common on quick AC top-ups). We treat
# a SoC rise while the odometer is stationary as an implicit charge.
SOC_OPEN_DELTA = 2  # %; threshold to OPEN a synthetic session (noise guard)
ODO_STATIONARY_TOL_KM = 0.5  # parking-spot wiggle / GPS drift tolerance


def _open_session(session: Session, vin: str) -> ChargeSession | None:
    stmt = (
        select(ChargeSession)
        .where(ChargeSession.vin == vin, ChargeSession.is_complete.is_(False))  # noqa: E712
        .order_by(ChargeSession.started_at.desc())
    )
    return session.exec(stmt).first()


def previous_snapshot(
    session: Session, vin: str, before: datetime
) -> VehicleSnapshot | None:
    """Most recent VehicleSnapshot strictly before `before` for this VIN.

    Public so scheduler.poll_once can do the lookup once and share the result
    with both update_session and run_anomaly_checks (avoids three identical
    indexed queries per poll)."""
    stmt = (
        select(VehicleSnapshot)
        .where(VehicleSnapshot.vin == vin, VehicleSnapshot.fetched_at < before)
        .order_by(VehicleSnapshot.fetched_at.desc())
        .limit(1)
    )
    return session.exec(stmt).first()


def _power_aggregates(
    session: Session, vin: str, start: datetime, end: datetime
) -> tuple[int | None, int | None]:
    """Compute (avg_w, peak_w) of charging_power_w across the session window.

    SQL-side SUM/COUNT/MAX is both faster and *correct*, replacing the old
    running-average hack that drifted with poll cadence."""
    stmt = select(
        func.avg(VehicleSnapshot.charging_power_w),
        func.max(VehicleSnapshot.charging_power_w),
    ).where(
        VehicleSnapshot.vin == vin,
        VehicleSnapshot.fetched_at >= start,
        VehicleSnapshot.fetched_at <= end,
        VehicleSnapshot.charging_power_w.is_not(None),
    )
    avg_w, peak_w = session.exec(stmt).one()
    return (
        int(avg_w) if avg_w is not None else None,
        int(peak_w) if peak_w is not None else None,
    )


def _battery_capacity_kwh(snap: Snapshot) -> float:
    """Best available pack capacity: the car's own spec when present, else the
    value calibrated from imported journey trips, else the hard-coded default.
    Mirrors tripbook so per-session and per-trip energy use the same basis."""
    if snap.battery_capacity_kwh:
        return float(snap.battery_capacity_kwh)
    return get_calibrated_battery_kwh(fallback=DEFAULT_BATTERY_KWH)


def _odo_stationary(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        # Missing odometer → assume stationary (don't block charge detection).
        return True
    return abs(a - b) <= ODO_STATIONARY_TOL_KM


def _auto_tag_station(cs: ChargeSession, db: Session) -> None:
    """Auto-fill note + price from a known ChargingStation if GPS matches."""
    if cs.note:
        return
    try:
        from .stations import find_station, locate_session
        coords = locate_session(cs, db=db)
        if coords is None:
            return
        station = find_station(*coords)
        if station is None:
            return
        cs.note = station.name
        if station.price_ct_per_kwh is not None and cs.price_ct_per_kwh is None:
            cs.price_ct_per_kwh = station.price_ct_per_kwh
        log.info("Auto-tagged session %s as '%s'", cs.id, station.name)
    except Exception:  # noqa: BLE001
        log.exception("Auto-tag station failed for session %s", cs.id)


def _should_close(
    cs: ChargeSession,
    status: str | None,
    snap: Snapshot,
    prev: VehicleSnapshot | None,
    stationary: bool,
    soc_delta: int | None,
) -> bool:
    """Whether the open session should be closed on this tick.

    API sessions close on any terminal status. Inferred (silent) sessions
    never see a terminal status — their status is "Idle" throughout — so a
    plateau (soc_delta == 0) must NOT close them, or a slow AC charge gets
    shredded into many one-tick sessions. They end only when the SoC actually
    falls, the car is driven away, or the 24 h safety timeout fires."""
    timed_out = (snap.fetched_at - cs.started_at) > timedelta(hours=24)
    if not cs.inferred:
        return status in TERMINAL_STATES or timed_out
    drove_off = prev is not None and not stationary
    soc_dropped = soc_delta is not None and soc_delta < 0
    return drove_off or soc_dropped or timed_out


def update_session(
    snap: Snapshot, *, prev: VehicleSnapshot | None = None
) -> ChargeSession | None:
    """Update the open ChargeSession for this VIN based on a new snapshot.

    Returns the session that was just *closed* (so the caller can notify), or
    None if nothing was closed this tick.

    Callers (scheduler.poll_once) may pass a pre-fetched `prev` snapshot to
    avoid duplicating the indexed lookup; other entry points let us fetch it.
    """
    status = snap.charging_status
    closed: ChargeSession | None = None

    with Session(get_engine()) as s:
        cs = _open_session(s, snap.vin)
        if prev is None:
            prev = previous_snapshot(s, snap.vin, snap.fetched_at)

        soc_delta = None
        if (
            prev is not None
            and snap.soc_percent is not None
            and prev.soc_percent is not None
        ):
            soc_delta = snap.soc_percent - prev.soc_percent

        stationary = prev is not None and _odo_stationary(
            snap.odometer_km, prev.odometer_km
        )
        # Effective "charging" signal: trust the API when it says so, otherwise
        # infer from a SoC rise while parked. Once a session is open, even a
        # +1% bump counts as continuation; to open a new one we want ≥ delta to
        # avoid noise (idle drain bounce, %-rounding).
        api_charging = status in CHARGING_STATES
        inferred_open = (
            cs is None
            and stationary
            and soc_delta is not None
            and soc_delta >= SOC_OPEN_DELTA
        )
        inferred_continue = (
            cs is not None
            and stationary
            and soc_delta is not None
            and soc_delta >= 1
        )
        is_charging = api_charging or inferred_open or inferred_continue

        if is_charging:
            if cs is None:
                from .co2 import co2_g_per_kwh

                # Backdate to the previous snapshot for inferred opens so the
                # session covers the actual charging window (and SoC delta
                # captures the first %-points we'd otherwise miss).
                if api_charging or prev is None:
                    started_at = snap.fetched_at
                    soc_start = snap.soc_percent
                    range_start = snap.range_km
                else:
                    started_at = prev.fetched_at
                    soc_start = prev.soc_percent
                    range_start = prev.range_km

                cs = ChargeSession(
                    vin=snap.vin,
                    started_at=started_at,
                    soc_start=soc_start,
                    range_start_km=range_start,
                    peak_power_w=snap.charging_power_w,
                    avg_power_w=snap.charging_power_w,
                    co2_g_per_kwh=co2_g_per_kwh(started_at),
                    inferred=not api_charging,
                )
                s.add(cs)
                s.commit()
                s.refresh(cs)
                log.info(
                    "Opened charge session for %s at SoC %s%% (%s)",
                    snap.vin,
                    soc_start,
                    "api" if api_charging else "inferred",
                )
            else:
                # Recompute avg/peak from the persisted snapshot stream
                # (cheap indexed scan on a few rows) — drift-free, cadence-
                # independent, replaces a running-average hack that silently
                # skewed toward the end of the session.
                avg_w, peak_w = _power_aggregates(
                    s, snap.vin, cs.started_at, snap.fetched_at
                )
                if avg_w is not None:
                    cs.avg_power_w = avg_w
                if peak_w is not None:
                    cs.peak_power_w = peak_w
                cs.soc_end = snap.soc_percent
                cs.range_end_km = snap.range_km
                # The API now confirms a real charge — promote a session we'd
                # opened only on a SoC rise so it closes on the normal terminal
                # state like any API session.
                if api_charging and cs.inferred:
                    cs.inferred = False
                s.add(cs)
                s.commit()

        elif cs is not None and (
            _should_close(cs, status, snap, prev, stationary, soc_delta)
        ):
            cs.ended_at = snap.fetched_at
            # Don't let post-charge driving regress the peak SoC/range we
            # already captured during the session (matters for inferred
            # sessions that close one tick after the car was unplugged + moved).
            if snap.soc_percent is not None:
                cs.soc_end = max(cs.soc_end or 0, snap.soc_percent)
            if snap.range_km is not None:
                cs.range_end_km = max(cs.range_end_km or 0, snap.range_km)
            # Final pass over the full session window in case the latest tick
            # delivered the actual peak/avg samples.
            avg_w, peak_w = _power_aggregates(
                s, snap.vin, cs.started_at, snap.fetched_at
            )
            if avg_w is not None:
                cs.avg_power_w = avg_w
            if peak_w is not None:
                cs.peak_power_w = peak_w
            if cs.soc_start is not None and cs.soc_end is not None:
                delta = max(0, cs.soc_end - cs.soc_start)
                net_kwh = delta / 100 * _battery_capacity_kwh(snap)
                cs.energy_kwh = round(net_kwh * get_charging_loss_factor(), 1)
            cs.is_complete = True
            _auto_tag_station(cs, s)
            s.add(cs)
            s.commit()
            s.refresh(cs)
            closed = cs
            log.info(
                "Closed charge session for %s: %s -> %s%% (~%s kWh, station=%s)",
                snap.vin,
                cs.soc_start,
                cs.soc_end,
                cs.energy_kwh,
                cs.note or "?",
            )

    return closed


def latest_dt(vin: str) -> datetime | None:
    with Session(get_engine()) as s:
        stmt = (
            select(VehicleSnapshot.fetched_at)
            .where(VehicleSnapshot.vin == vin)
            .order_by(VehicleSnapshot.fetched_at.desc())
            .limit(1)
        )
        return s.exec(stmt).first()
