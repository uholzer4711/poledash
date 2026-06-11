"""Attach C3Snapshot measurements (GPS, on-car temperature, cabin PM2.5) to
other domain objects: charge sessions, detected trips, imported journeys.

Single place for the "nearest snapshot in a time window" / "points between two
times" queries so sessions.py, the /api/trips endpoint and the JourneyImport
enrichment don't each re-implement them.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from .db.models import C3Snapshot, ChargeSession, JourneyImport
from .db.session import get_engine

log = logging.getLogger(__name__)


def nearest_c3_env(
    start: datetime, end: datetime | None, *, db: Session | None = None, pad_min: int = 30
) -> dict | None:
    """Nearest C3 measurements around [start, end] (± pad). Returns
    {lat, lng, temp_c, pm25, ts} with Nones where a field has no sample in the
    window, or None when the window is empty. temp/pm25 come from the nearest
    row that actually carries each (the C3 weather/air sections can fail
    independently of the GPS one).

    lat/lng are picked by the *fix* timestamp (gps_ts), not by when we polled:
    C3 freezes GPS while driving, so a row fetched mid-drive still carries the
    coordinates of the previous parking spot. The newest fix recorded no later
    than the window's end is the position the car actually held during the
    window; rows whose only fixes were made after the window (car already
    re-parked elsewhere) yield no GPS rather than a wrong one."""
    pad = timedelta(minutes=pad_min)
    lo = start - pad
    hi = (end or start) + pad
    # Small slack over `end` itself: the arrival fix for the event can be
    # stamped seconds after our naive-UTC window edge (clock skew).
    fix_bound = (end or start) + timedelta(minutes=2)

    def _fix_dt(r: C3Snapshot) -> datetime | None:
        if not r.gps_ts:
            return None
        return datetime.fromtimestamp(r.gps_ts, UTC).replace(tzinfo=None)

    def _q(s: Session) -> dict | None:
        rows = list(
            s.exec(
                select(C3Snapshot)
                .where(C3Snapshot.fetched_at >= lo)
                .where(C3Snapshot.fetched_at <= hi)
                .order_by(C3Snapshot.fetched_at.asc())
            ).all()
        )
        if not rows:
            return None

        def nearest(cands: list[C3Snapshot]) -> C3Snapshot | None:
            return (
                min(cands, key=lambda r: abs((r.fetched_at - start).total_seconds()))
                if cands
                else None
            )

        gps_cands = [r for r in rows if r.lat is not None and r.lng is not None]
        with_fix = [r for r in gps_cands if _fix_dt(r) is not None]
        valid_fix = [r for r in with_fix if _fix_dt(r) <= fix_bound]
        if valid_fix:
            gps = max(valid_fix, key=lambda r: r.gps_ts)
        elif with_fix:
            gps = None  # only post-window fixes → position during window unknown
        else:
            gps = nearest(gps_cands)  # legacy rows without gps_ts: old behaviour
        tmp = nearest([r for r in rows if r.weather_temp_c is not None])
        pm = nearest([r for r in rows if r.air_pm25 is not None])
        if gps is None and tmp is None and pm is None:
            return None
        return {
            "lat": gps.lat if gps else None,
            "lng": gps.lng if gps else None,
            "temp_c": tmp.weather_temp_c if tmp else None,
            "pm25": pm.air_pm25 if pm else None,
            "ts": (gps or tmp or pm).fetched_at,
        }

    if db is not None:
        return _q(db)
    with Session(get_engine()) as s:
        return _q(s)


def c3_path_between(
    start: datetime, end: datetime, *, db: Session | None = None
) -> list[dict]:
    """Ordered GPS breadcrumb [{lat,lng,ts,speed}] recorded in [start, end]."""

    def _q(s: Session) -> list[dict]:
        rows = s.exec(
            select(C3Snapshot.lat, C3Snapshot.lng, C3Snapshot.fetched_at, C3Snapshot.speed)
            .where(C3Snapshot.fetched_at >= start)
            .where(C3Snapshot.fetched_at <= end)
            .where(C3Snapshot.lat.is_not(None))
            .where(C3Snapshot.lng.is_not(None))
            .order_by(C3Snapshot.fetched_at.asc())
        ).all()
        return [
            {"lat": lat, "lng": lng, "ts": ts.isoformat(), "speed": speed}
            for lat, lng, ts, speed in rows
        ]

    if db is not None:
        return _q(db)
    with Session(get_engine()) as s:
        return _q(s)


def c3_env_aggregate(
    start: datetime, end: datetime, *, db: Session | None = None
) -> dict:
    """Mean on-car temperature + cabin PM2.5 across a window (None if no data)."""

    def _q(s: Session) -> dict:
        rows = s.exec(
            select(C3Snapshot.weather_temp_c, C3Snapshot.air_pm25)
            .where(C3Snapshot.fetched_at >= start)
            .where(C3Snapshot.fetched_at <= end)
        ).all()
        temps = [t for t, _ in rows if t is not None]
        pms = [p for _, p in rows if p is not None]
        return {
            "avg_temp_c": round(sum(temps) / len(temps), 1) if temps else None,
            "avg_pm25": round(sum(pms) / len(pms), 1) if pms else None,
            "n": len(rows),
        }

    if db is not None:
        return _q(db)
    with Session(get_engine()) as s:
        return _q(s)


def enrich_journey_imports_from_c3(limit: int = 200) -> dict:
    """Fill JourneyImport.air_pm25 (and prefer on-car temp over the Open-Meteo
    lookup) from the nearest C3 snapshot around each trip. Only rows that still
    lack PM2.5 are processed; rows with no nearby C3 data stay as-is and the
    Open-Meteo fallback keeps their temperature."""
    updated = 0
    checked = 0
    with Session(get_engine()) as s:
        rows = list(
            s.exec(
                select(JourneyImport)
                .where(JourneyImport.air_pm25.is_(None))
                .order_by(JourneyImport.started_at.desc())
                .limit(limit)
            ).all()
        )
        for r in rows:
            checked += 1
            env = nearest_c3_env(r.started_at, r.ended_at, db=s)
            if not env:
                continue
            changed = False
            if env.get("pm25") is not None:
                r.air_pm25 = env["pm25"]
                changed = True
            if env.get("temp_c") is not None:
                r.outside_temp_c = env["temp_c"]  # on-car temp beats the lookup
                changed = True
            if changed:
                s.add(r)
                updated += 1
        s.commit()
    return {"checked": checked, "updated": updated}


def backfill_session_c3_env(limit: int = 500) -> dict:
    """Stamp complete charge sessions that still lack on-car temp/PM2.5/GPS from
    the nearest C3 snapshot. Only fills empty fields, so a re-run never clobbers
    a value already captured. Mirrors sessions._stamp_c3_env for the close-time
    path; this is the catch-up for sessions that closed before C3 had the
    window (or before C3 was enabled)."""
    updated = 0
    checked = 0
    with Session(get_engine()) as s:
        rows = list(
            s.exec(
                select(ChargeSession)
                .where(ChargeSession.is_complete.is_(True))  # noqa: E712
                .where(ChargeSession.outside_temp_c.is_(None))
                .order_by(ChargeSession.started_at.desc())
                .limit(limit)
            ).all()
        )
        for cs in rows:
            checked += 1
            env = nearest_c3_env(cs.started_at, cs.ended_at, db=s)
            if not env:
                continue
            changed = False
            if cs.lat is None and env.get("lat") is not None:
                cs.lat = env["lat"]
                cs.lng = env["lng"]
                changed = True
            if cs.outside_temp_c is None and env.get("temp_c") is not None:
                cs.outside_temp_c = env["temp_c"]
                changed = True
            if cs.air_pm25 is None and env.get("pm25") is not None:
                cs.air_pm25 = env["pm25"]
                changed = True
            if changed:
                s.add(cs)
                updated += 1
        s.commit()
    return {"checked": checked, "updated": updated}
