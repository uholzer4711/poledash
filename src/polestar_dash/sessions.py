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


def _open_session(session: Session, vin: str) -> ChargeSession | None:
    stmt = (
        select(ChargeSession)
        .where(ChargeSession.vin == vin, ChargeSession.is_complete.is_(False))  # noqa: E712
        .order_by(ChargeSession.started_at.desc())
    )
    return session.exec(stmt).first()


def update_session(snap: Snapshot) -> ChargeSession | None:
    """Update the open ChargeSession for this VIN based on a new snapshot.

    Returns the session that was just *closed* (so the caller can notify), or
    None if nothing was closed this tick.
    """
    status = snap.charging_status
    closed: ChargeSession | None = None

    with Session(get_engine()) as s:
        cs = _open_session(s, snap.vin)

        if status in CHARGING_STATES:
            if cs is None:
                from .co2 import co2_g_per_kwh

                cs = ChargeSession(
                    vin=snap.vin,
                    started_at=snap.fetched_at,
                    soc_start=snap.soc_percent,
                    range_start_km=snap.range_km,
                    peak_power_w=snap.charging_power_w,
                    avg_power_w=snap.charging_power_w,
                    co2_g_per_kwh=co2_g_per_kwh(snap.fetched_at),
                )
                s.add(cs)
                s.commit()
                s.refresh(cs)
                log.info("Opened charge session for %s at SoC %s%%", snap.vin, snap.soc_percent)
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
            cs.soc_end = snap.soc_percent
            cs.range_end_km = snap.range_km
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
