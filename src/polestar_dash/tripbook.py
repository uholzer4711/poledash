"""Lightweight trip detection from the odometer + charging-status stream.

A trip = continuous interval where the odometer is increasing and the car is
not charging. Since polling is sparse, we collapse close samples (<2 h gap)
into one trip and aggregate distance/duration/start-end odometer/SoC."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from .util import utcnow

from sqlmodel import Session, select

from .app_settings import get_calibrated_battery_kwh
from .db.models import VehicleSnapshot
from .db.session import get_engine
from .sessions import DEFAULT_BATTERY_KWH

_battery_kwh_cache: tuple[float, float] | None = None  # (value, timestamp)
_BATTERY_KWH_TTL = 300  # 5 minutes


def _battery_kwh() -> float:
    """Best available battery capacity in kWh (cached 5 min).

    Preference order:
    1. `battery_capacity_kwh` from the most recent snapshot (the car itself
       reports the spec — exact, no estimation).
    2. The calibrated value derived from imported journey-log CSV trips.
    3. The hard-coded DEFAULT_BATTERY_KWH as a last resort.
    """
    global _battery_kwh_cache
    now = time.monotonic()
    if _battery_kwh_cache is not None and (now - _battery_kwh_cache[1]) < _BATTERY_KWH_TTL:
        return _battery_kwh_cache[0]

    with Session(get_engine()) as s:
        stmt = (
            select(VehicleSnapshot.battery_capacity_kwh)
            .where(VehicleSnapshot.battery_capacity_kwh.is_not(None))
            .order_by(VehicleSnapshot.fetched_at.desc())
            .limit(1)
        )
        cap = s.exec(stmt).first()
    val = float(cap) if cap else get_calibrated_battery_kwh(fallback=DEFAULT_BATTERY_KWH)
    _battery_kwh_cache = (val, now)
    return val


@dataclass
class Trip:
    started_at: datetime
    ended_at: datetime
    start_km: float
    end_km: float
    soc_start: int | None = None
    soc_end: int | None = None
    avg_consumption_kwh_per_100km: float | None = None

    @property
    def km(self) -> float:
        return round(max(0.0, self.end_km - self.start_km), 1)

    @property
    def duration_min(self) -> int:
        return int((self.ended_at - self.started_at).total_seconds() // 60)

    @property
    def kwh_used(self) -> float | None:
        # Prefer the car's own reported efficiency. SoC is quantized to whole
        # percent, so on a short trip a single 1% step (~0.8 kWh) swamps the
        # real energy and the SoC estimate reads up to ~2x high. The car's
        # avg-consumption figure is what its trip computer shows.
        if self.avg_consumption_kwh_per_100km is not None and self.km > 0.5:
            return round(self.avg_consumption_kwh_per_100km * self.km / 100.0, 2)
        if self.soc_start is None or self.soc_end is None:
            return None
        drop = self.soc_start - self.soc_end
        if drop <= 0:
            return None
        return round(drop / 100.0 * _battery_kwh(), 2)

    @property
    def wh_per_km(self) -> int | None:
        kwh = self.kwh_used
        if kwh is None or self.km <= 0.5:
            return None
        return int(round(kwh * 1000 / self.km))

    @property
    def kwh_per_100km(self) -> float | None:
        wh = self.wh_per_km
        return round(wh / 10, 1) if wh is not None else None


MAX_PLAUSIBLE_KMH = 180  # cap to filter out odo jumps after long idle gaps


def _odo_event_or_fetch(r: VehicleSnapshot) -> datetime:
    """Best available timestamp for when the odometer reading happened."""
    return r.odometer_event_ts or r.fetched_at


def detect_trips(vin: str | None = None, days: int = 30, gap_minutes: int = 10) -> list[Trip]:
    """Detect trips from the snapshot stream.

    A new trip starts when the odometer ticks again after `gap_minutes` of
    no movement (or after a charging episode). Idle time is measured from
    the *odometer event timestamp* the car reports — not from our poll
    cadence — so the threshold can be shorter than the poll interval.
    """
    since = utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        stmt = (
            select(VehicleSnapshot)
            .where(VehicleSnapshot.fetched_at >= since)
            .where(VehicleSnapshot.odometer_km.is_not(None))
            .order_by(VehicleSnapshot.fetched_at.asc())
        )
        if vin:
            stmt = stmt.where(VehicleSnapshot.vin == vin)
        rows = list(s.exec(stmt).all())

    trips: list[Trip] = []
    cur: Trip | None = None
    prev: VehicleSnapshot | None = None
    last_move_ts: datetime | None = None  # wall-clock of last odometer tick
    # Most recent `odometer_event_ts` we've observed. Used to spot phantom
    # odometer jumps: the Polestar API occasionally back-fills a stale
    # odometer reading hours after the actual movement, without bumping the
    # event timestamp. Without this guard the back-fill looks like a fresh
    # trip at poll time.
    last_event_ts: datetime | None = None
    # SoC samples observed since the odometer last ticked. Used to recover the
    # true pre-trip SoC: the Polestar battery endpoint updates several minutes
    # before the odometer catches up, so by the time `moved` is detected, the
    # `prev` row's SoC is already mid-drop. We take the max SoC inside a
    # 60-minute window before trip_start — that survives pre-conditioning and
    # SoC-report latency without attributing slow vampire drain to the trip.
    parked_soc_hist: list[tuple[datetime, int]] = []

    def record_parked_soc(rec: VehicleSnapshot) -> None:
        if rec.soc_percent is None:
            return
        # fetched_at (not battery_event_ts): we want the SoC the car was *at*
        # when we observed it, not when the car says the reading was taken.
        # Otherwise a stale battery_event_ts pushes good readings out of the
        # lookback window.
        parked_soc_hist.append((rec.fetched_at, rec.soc_percent))

    def soc_at_trip_start(fallback: int | None, trip_start: datetime) -> int | None:
        window_start = trip_start - timedelta(minutes=60)
        recent = [s for t, s in parked_soc_hist if t >= window_start]
        if recent:
            return max(recent)
        return fallback

    def close_trip() -> None:
        nonlocal cur
        if cur is not None and cur.km > 0:
            trips.append(cur)
        cur = None

    for r in rows:
        odo = r.odometer_km
        if odo is None:
            continue

        if prev is None:
            prev = r
            record_parked_soc(r)
            continue

        is_charging = r.charging_status in ("Charging", "Smart Charging")
        gap = (r.fetched_at - prev.fetched_at).total_seconds() / 60
        dkm = odo - (prev.odometer_km or odo)
        value_moved = odo > (prev.odometer_km or odo)

        evt = r.odometer_event_ts
        prev_evt = prev.odometer_event_ts

        # The car's own odometer-reading timestamp only advances while it is
        # moving and freezes when parked. An advancing event timestamp is
        # therefore a movement signal in its own right — and a more timely one
        # than the odometer *value*, which Polestar refreshes minutes later and
        # in batches. Without this, a drive whose odometer ticks just once is
        # detected as a few-minute trip even though the event timestamps show
        # it ran far longer.
        event_advanced = evt is not None and (
            last_event_ts is None or evt > last_event_ts
        )

        # Phantom-moved guard: odometer value jumped but the event timestamp
        # didn't advance past the last one we saw — Polestar is back-filling an
        # older reading, not reporting new motion.
        if (
            value_moved
            and evt is not None
            and last_event_ts is not None
            and evt <= last_event_ts
        ):
            value_moved = False

        moved = value_moved or event_advanced

        if evt is not None and (last_event_ts is None or evt > last_event_ts):
            last_event_ts = evt

        # Idle = wall-clock minutes since the last poll at which we *observed*
        # motion. Measured in fetched_at space (not event-ts) so the poll
        # cadence and the event-timestamp lag don't make a continuous drive
        # look like a string of sub-threshold hops.
        idle_min = (
            (r.fetched_at - last_move_ts).total_seconds() / 60
            if last_move_ts is not None
            else 0.0
        )

        # Plausibility: an implausibly high implied speed across a poll gap
        # means the car was idle most of it and the odometer jump is a
        # back-fill. Recover the real start from the event-ts window if it
        # checks out; otherwise skip the sample.
        forced_start: datetime | None = None
        if value_moved and dkm > 0:
            implied_kmh = dkm / max(gap / 60.0, 1e-6)
            if implied_kmh > MAX_PLAUSIBLE_KMH:
                if evt and prev_evt and evt > prev_evt:
                    window_min = (evt - prev_evt).total_seconds() / 60
                    if window_min > 0 and dkm / (window_min / 60.0) <= 250:
                        forced_start = prev_evt
                    else:
                        prev = r
                        continue
                else:
                    prev = r
                    continue

        # Close the open trip if the car has been idle past the threshold or
        # has started charging since the last observed movement.
        if cur is not None and (idle_min > gap_minutes or is_charging) and not moved:
            close_trip()

        # A `moved` sample is a trip event even when the car is now
        # `Charging` — the drive happened in the past, the plug-in just
        # followed immediately. Filtering charging out here lost the trip.
        if moved:
            # The event timestamp is when the car last recorded this motion:
            # for the first motion poll of a trip it is ≈ the start; for each
            # subsequent poll it tracks the running end.
            motion_ts = evt or r.fetched_at
            if cur is None:
                # Start anchor: when the odometer *value* jumped across this
                # poll gap the drive happened somewhere inside it, so anchor to
                # the previous (parked) poll. When motion was detected purely
                # from an advancing event timestamp (odometer value still
                # flat), that event timestamp *is* the start.
                if value_moved:
                    trip_start = forced_start or prev.fetched_at
                else:
                    trip_start = motion_ts
                cur = Trip(
                    started_at=trip_start,
                    ended_at=motion_ts,
                    start_km=prev.odometer_km or odo,
                    end_km=odo,
                    soc_start=soc_at_trip_start(prev.soc_percent, trip_start),
                    soc_end=r.soc_percent,
                    avg_consumption_kwh_per_100km=r.avg_consumption_kwh_per_100km
                    or prev.avg_consumption_kwh_per_100km,
                )
            else:
                cur.ended_at = motion_ts
                cur.end_km = odo
                cur.soc_end = r.soc_percent
                if r.avg_consumption_kwh_per_100km is not None:
                    cur.avg_consumption_kwh_per_100km = r.avg_consumption_kwh_per_100km
            last_move_ts = r.fetched_at
            parked_soc_hist.clear()
            record_parked_soc(r)
        else:
            record_parked_soc(r)

        prev = r

    if cur is not None and cur.km > 0:
        trips.append(cur)

    # Post-process: trim implausible trip durations using a typical avg speed.
    #  - Too SLOW (avg < 8 km/h): detector merged a parked interval into the
    #    trip — Polestar sometimes back-fills the odometer hours after the
    #    drive and bumps the event timestamp to "now", sneaking past the
    #    phantom-moved guard.
    #  - Too FAST (avg > 180 km/h): the snapshot at trip start was stale
    #    (battery/odo API not yet refreshed at poll time), so prev.fetched_at
    #    is later than when the car actually started moving. Push the start
    #    backwards to a plausible estimate.
    TYPICAL_AVG_KMH = 40.0
    MIN_PLAUSIBLE_AVG_KMH = 8.0
    prev_end: datetime | None = None
    for t in trips:
        duration_h = (t.ended_at - t.started_at).total_seconds() / 3600
        if duration_h > 0 and t.km > 0:
            avg_kmh = t.km / duration_h
            if avg_kmh < MIN_PLAUSIBLE_AVG_KMH or avg_kmh > MAX_PLAUSIBLE_KMH:
                plausible_min = (t.km / TYPICAL_AVG_KMH) * 60
                estimated = t.ended_at - timedelta(minutes=plausible_min)
                # Never push the start before the previous trip's end —
                # otherwise typical-speed estimates can overshoot a known
                # idle interval and overlap into the prior trip.
                if prev_end is not None and estimated < prev_end:
                    estimated = prev_end
                t.started_at = estimated
        prev_end = t.ended_at

    return trips


def monthly_totals(trips: list[Trip]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for t in trips:
        key = t.started_at.strftime("%Y-%m")
        out.setdefault(key, {"trips": 0, "km": 0.0, "minutes": 0, "kwh": 0.0})
        out[key]["trips"] += 1
        out[key]["km"] += t.km
        out[key]["minutes"] += t.duration_min
        if t.kwh_used is not None:
            out[key]["kwh"] += t.kwh_used
    for v in out.values():
        v["km"] = round(v["km"], 1)
        v["kwh"] = round(v["kwh"], 1)
        v["avg_kwh_per_100km"] = (
            round(v["kwh"] * 100 / v["km"], 1) if v["km"] > 0 and v["kwh"] > 0 else None
        )
    return out
