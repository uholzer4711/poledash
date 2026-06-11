"""Detect non-trip battery drain (pre-conditioning / standheizung / vampire drain).

Heuristic: between two consecutive snapshots, the car was NOT charging and the
odometer did NOT change, but SoC dropped. That energy went somewhere — usually
HVAC pre-conditioning, in-car services, or normal parasitic drain.

We collapse adjacent idle-drop samples into one event (gap ≤ 6 h) so a single
warm-up session shows as one row instead of three poll-interval slivers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from .app_settings import get_calibrated_battery_kwh
from .db.models import C3Snapshot, VehicleSnapshot
from .db.session import get_engine
from .util import utcnow

GAP_MINUTES = 360  # 6 h


@dataclass
class IdleDrain:
    started_at: datetime
    ended_at: datetime
    soc_start: int
    soc_end: int
    duration_min: int
    # Filled by _attribute() from C3 rows overlapping the drain window:
    # "climate" (pre-conditioning ran), "air" (cabin pre-cleaning ran),
    # "standby" (C3 saw the window, nothing was active → parasitic loss),
    # None (no C3 coverage → unknown).
    cause: str | None = None
    # Minutes the car had already been parked when the drain started
    # (from the C3 parked_ts fix timestamp) — vampire drain shows up hours
    # after parking, conditioning right around departure/arrival.
    parked_for_min: int | None = None

    @property
    def soc_drop(self) -> int:
        return self.soc_start - self.soc_end

    @property
    def kwh(self) -> float:
        return round(self.soc_drop / 100.0 * get_calibrated_battery_kwh(), 2)


def detect_idle_drains(vin: str | None = None, days: int = 30) -> list[IdleDrain]:
    since = utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        stmt = (
            select(VehicleSnapshot)
            .where(VehicleSnapshot.fetched_at >= since)
            .where(VehicleSnapshot.soc_percent.is_not(None))
            .order_by(VehicleSnapshot.fetched_at.asc())
        )
        if vin:
            stmt = stmt.where(VehicleSnapshot.vin == vin)
        rows = list(s.exec(stmt).all())

    out: list[IdleDrain] = []
    cur: IdleDrain | None = None
    prev: VehicleSnapshot | None = None

    for r in rows:
        if prev is None:
            prev = r
            continue

        gap_min = (r.fetched_at - prev.fetched_at).total_seconds() / 60
        is_charging = r.charging_status in ("Charging", "Smart Charging")
        odo_same = (
            prev.odometer_km is not None
            and r.odometer_km is not None
            and abs(r.odometer_km - prev.odometer_km) < 0.05
        )
        soc_dropped = (
            prev.soc_percent is not None
            and r.soc_percent is not None
            and r.soc_percent < prev.soc_percent
        )

        if soc_dropped and odo_same and not is_charging and gap_min <= GAP_MINUTES:
            if cur is None:
                cur = IdleDrain(
                    started_at=prev.fetched_at,
                    ended_at=r.fetched_at,
                    soc_start=prev.soc_percent,
                    soc_end=r.soc_percent,
                    duration_min=int(gap_min),
                )
            else:
                cur.ended_at = r.fetched_at
                cur.soc_end = r.soc_percent
                cur.duration_min = int(
                    (r.fetched_at - cur.started_at).total_seconds() // 60
                )
        else:
            if cur is not None:
                out.append(cur)
                cur = None

        prev = r

    if cur is not None:
        out.append(cur)
    # Drop noise: 1% drop over many hours is just background parasitic loss.
    # Pre-conditioning typically drops 2-5% in 15-60 min.
    drains = [d for d in out if d.soc_drop >= 2]
    _attribute(drains)
    return drains


# Climate states that mean "the HVAC is (about to be) doing work" — see
# c3._CLIMA. climate_active alone misses the spin-up polls, where energy
# already flows but is_active is still False.
_CLIMATE_ACTIVE_STATES = {
    "starting", "starting (delay)", "external power", "active",
    "active + pre-cleaning", "heater active",
}


def _attribute(drains: list[IdleDrain]) -> None:
    """Fill cause + parked_for_min from the C3 rows overlapping each drain.

    The window is padded by ±10 min: C3 polls every 5–30 min, so a short
    pre-conditioning burst can fall just outside the snapshot pair that framed
    the SoC drop. Drains predating C3 history simply stay cause=None."""
    if not drains:
        return
    pad = timedelta(minutes=10)
    with Session(get_engine()) as s:
        for d in drains:
            rows = list(
                s.exec(
                    select(C3Snapshot)
                    .where(C3Snapshot.fetched_at >= d.started_at - pad)
                    .where(C3Snapshot.fetched_at <= d.ended_at + pad)
                    .order_by(C3Snapshot.fetched_at.asc())
                ).all()
            )
            if not rows:
                continue
            climate = any(
                r.climate_active or (r.climate_running in _CLIMATE_ACTIVE_STATES)
                for r in rows
            )
            air = any(r.air_running for r in rows)
            d.cause = "climate" if climate else ("air" if air else "standby")
            # Parked-for: the parked fix is stamped when the car parks and
            # stays put, so the first overlapping row carries the park time.
            ts = rows[0].parked_ts or rows[0].gps_ts
            if ts:
                parked_at = datetime.fromtimestamp(ts, UTC).replace(tzinfo=None)
                if parked_at <= d.started_at:
                    d.parked_for_min = int(
                        (d.started_at - parked_at).total_seconds() // 60
                    )
