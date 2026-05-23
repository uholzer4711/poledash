"""Charging session detection from the snapshot stream."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlmodel import Session, select

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


def _previous_snapshot(
    session: Session, vin: str, before: datetime
) -> VehicleSnapshot | None:
    stmt = (
        select(VehicleSnapshot)
        .where(VehicleSnapshot.vin == vin, VehicleSnapshot.fetched_at < before)
        .order_by(VehicleSnapshot.fetched_at.desc())
        .limit(1)
    )
    return session.exec(stmt).first()


def _odo_stationary(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        # Missing odometer → assume stationary (don't block charge detection).
        return True
    return abs(a - b) <= ODO_STATIONARY_TOL_KM


def update_session(snap: Snapshot) -> ChargeSession | None:
    """Update the open ChargeSession for this VIN based on a new snapshot.

    Returns the session that was just *closed* (so the caller can notify), or
    None if nothing was closed this tick.
    """
    status = snap.charging_status
    closed: ChargeSession | None = None

    with Session(get_engine()) as s:
        cs = _open_session(s, snap.vin)
        prev = _previous_snapshot(s, snap.vin, snap.fetched_at)

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
                if snap.charging_power_w and (
                    cs.peak_power_w is None or snap.charging_power_w > cs.peak_power_w
                ):
                    cs.peak_power_w = snap.charging_power_w
                # True running average across all observed ticks during the
                # session: estimate sample count from start..now / poll
                # interval and use it to weight the new sample. The old
                # (a+b)//2 form weighted the latest sample exponentially and
                # silently skewed the avg toward the end of the session.
                if snap.charging_power_w is not None:
                    if cs.avg_power_w is None:
                        cs.avg_power_w = snap.charging_power_w
                    else:
                        elapsed_min = max(
                            1,
                            (snap.fetched_at - cs.started_at).total_seconds() / 60,
                        )
                        # Best guess at sample count for this session so far
                        # (fast-poll cadence while charging). Clamped to ≥ 1.
                        approx_n = max(1, int(elapsed_min / 2))
                        cs.avg_power_w = int(
                            (cs.avg_power_w * approx_n + snap.charging_power_w)
                            / (approx_n + 1)
                        )
                cs.soc_end = snap.soc_percent
                cs.range_end_km = snap.range_km
                s.add(cs)
                s.commit()

        elif cs is not None and status in TERMINAL_STATES:
            cs.ended_at = snap.fetched_at
            # Don't let post-charge driving regress the peak SoC/range we
            # already captured during the session (matters for inferred
            # sessions that close one tick after the car was unplugged + moved).
            if snap.soc_percent is not None:
                cs.soc_end = max(cs.soc_end or 0, snap.soc_percent)
            if snap.range_km is not None:
                cs.range_end_km = max(cs.range_end_km or 0, snap.range_km)
            if cs.soc_start is not None and cs.soc_end is not None:
                delta = max(0, cs.soc_end - cs.soc_start)
                cs.energy_kwh = round(delta / 100 * DEFAULT_BATTERY_KWH, 1)
            cs.is_complete = True
            s.add(cs)
            s.commit()
            s.refresh(cs)
            closed = cs
            log.info(
                "Closed charge session for %s: %s -> %s%% (~%s kWh)",
                snap.vin,
                cs.soc_start,
                cs.soc_end,
                cs.energy_kwh,
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
