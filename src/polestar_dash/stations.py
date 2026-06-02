"""Charging-station registry: GPS-based auto-fill for note + price."""

from __future__ import annotations

import math
from bisect import bisect_left
from datetime import datetime, timedelta

from sqlmodel import Session, select

from .db.models import ChargeSession, ChargingStation, JourneyImport
from .db.session import get_engine
from .util import utcnow

MATCH_RADIUS_M = 500


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6_371_000
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlng / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _c3_location(session: ChargeSession, s: Session) -> tuple[float, float] | None:
    """Direct GPS for a charge session from C3Snapshot — the car reports its own
    position, so when we have a C3 fix during (or just around) the session this
    beats the JourneyImport trip-endpoint heuristic."""
    from .c3_enrich import nearest_c3_env

    env = nearest_c3_env(session.started_at, session.ended_at, db=s)
    if env and env.get("lat") is not None:
        return (env["lat"], env["lng"])
    return None


def locate_session(
    session: ChargeSession, *, db: Session | None = None
) -> tuple[float, float] | None:
    """Infer GPS for a charge session.

    Persisted session GPS first (stamped at close), then a live C3Snapshot
    lookup, then the JourneyImport trip-endpoint heuristic as a fallback for
    sessions older than C3 polling."""
    window = timedelta(hours=4)

    def _query(s: Session):
        if session.lat is not None and session.lng is not None:
            return (session.lat, session.lng)
        c3 = _c3_location(session, s)
        if c3 is not None:
            return c3
        imp_stmt = select(JourneyImport).where(
            JourneyImport.started_at >= session.started_at - timedelta(days=2),
            JourneyImport.started_at <= (session.ended_at or session.started_at) + timedelta(days=2),
        )
        imports = s.exec(imp_stmt).all()

        ends = sorted(
            (j for j in imports if j.end_lat is not None and j.end_lng is not None),
            key=lambda j: j.ended_at,
        )
        end_keys = [j.ended_at for j in ends]
        starts = sorted(
            (j for j in imports if j.start_lat is not None and j.start_lng is not None),
            key=lambda j: j.started_at,
        )
        start_keys = [j.started_at for j in starts]

        def _nearest(keys: list[datetime], rows: list, target: datetime):
            if not keys:
                return None
            i = bisect_left(keys, target)
            best, best_diff = None, window
            for ci in (i - 1, i):
                if 0 <= ci < len(rows):
                    diff = abs(keys[ci] - target)
                    if diff <= best_diff:
                        best_diff = diff
                        best = rows[ci]
            return best

        j = _nearest(end_keys, ends, session.started_at)
        if j is not None:
            return (j.end_lat, j.end_lng)
        ref = session.ended_at or session.started_at
        j = _nearest(start_keys, starts, ref)
        if j is not None:
            return (j.start_lat, j.start_lng)
        return None

    if db is not None:
        return _query(db)
    with Session(get_engine()) as s:
        return _query(s)


def find_station(lat: float, lng: float) -> ChargingStation | None:
    """Find the closest station within MATCH_RADIUS_M, or None."""
    with Session(get_engine()) as s:
        stations = s.exec(select(ChargingStation)).all()
    best, best_dist = None, MATCH_RADIUS_M
    for st in stations:
        if st.lat is None or st.lng is None:
            continue  # name-only station — no GPS to match against
        d = _haversine_m(lat, lng, st.lat, st.lng)
        if d <= best_dist:
            best_dist = d
            best = st
    return best


def upsert_station(
    name: str, lat: float, lng: float, price_ct_per_kwh: float | None
) -> ChargingStation:
    """Create or update a station at the given location (500 m match).

    Matches by GPS proximity first; failing that, by exact name so a name-only
    entry (added manually, no GPS) gets upgraded with coordinates instead of
    spawning a duplicate.
    """
    with Session(get_engine()) as s:
        stations = s.exec(select(ChargingStation)).all()
        best, best_dist = None, MATCH_RADIUS_M
        for st in stations:
            if st.lat is None or st.lng is None:
                continue
            d = _haversine_m(lat, lng, st.lat, st.lng)
            if d <= best_dist:
                best_dist = d
                best = st

        if best is None:
            best = next(
                (st for st in stations if st.name.casefold() == name.casefold()),
                None,
            )

        if best is not None:
            best.name = name
            best.lat = lat
            best.lng = lng
            if price_ct_per_kwh is not None:
                best.price_ct_per_kwh = price_ct_per_kwh
            best.updated_at = utcnow()
            s.add(best)
            s.commit()
            s.refresh(best)
            return best

        station = ChargingStation(
            name=name, lat=lat, lng=lng, price_ct_per_kwh=price_ct_per_kwh
        )
        s.add(station)
        s.commit()
        s.refresh(station)
        return station


def upsert_named_station(
    name: str, price_ct_per_kwh: float | None
) -> ChargingStation:
    """Create or update a known place by name only (no GPS required).

    Used when the user adds a place manually (session form / settings) without
    any imported trip to anchor coordinates. If a station with the same name
    already exists (GPS or not), its price is updated rather than duplicated.
    """
    name = name.strip()
    with Session(get_engine()) as s:
        existing = s.exec(select(ChargingStation)).all()
        match = next(
            (st for st in existing if st.name.casefold() == name.casefold()), None
        )
        if match is not None:
            match.name = name
            if price_ct_per_kwh is not None:
                match.price_ct_per_kwh = price_ct_per_kwh
            match.updated_at = utcnow()
            s.add(match)
            s.commit()
            s.refresh(match)
            return match
        station = ChargingStation(name=name, price_ct_per_kwh=price_ct_per_kwh)
        s.add(station)
        s.commit()
        s.refresh(station)
        return station


def suggest_for_session(session: ChargeSession) -> dict | None:
    """Return {"name": ..., "price_ct_per_kwh": ...} if a known station matches."""
    coords = locate_session(session)
    if coords is None:
        return None
    station = find_station(*coords)
    if station is None:
        return None
    return {
        "station_id": station.id,
        "name": station.name,
        "price_ct_per_kwh": station.price_ct_per_kwh,
    }


def batch_suggest(sessions: list[ChargeSession]) -> dict[int, dict]:
    """Suggest stations for multiple sessions in one pass (preloads data)."""
    unfilled = [cs for cs in sessions if cs.note is None]
    if not unfilled:
        return {}

    with Session(get_engine()) as s:
        stations = s.exec(select(ChargingStation)).all()
        if not stations:
            return {}

        min_start = min(cs.started_at for cs in unfilled)
        max_end = max(cs.ended_at or cs.started_at for cs in unfilled)
        imp_stmt = select(JourneyImport).where(
            JourneyImport.started_at >= min_start - timedelta(days=2),
            JourneyImport.started_at <= max_end + timedelta(days=2),
        )
        imports = s.exec(imp_stmt).all()

    if not imports:
        return {}

    ends_sorted = sorted(
        (j for j in imports if j.end_lat is not None and j.end_lng is not None),
        key=lambda j: j.ended_at,
    )
    end_keys = [j.ended_at for j in ends_sorted]
    starts_sorted = sorted(
        (j for j in imports if j.start_lat is not None and j.start_lng is not None),
        key=lambda j: j.started_at,
    )
    start_keys = [j.started_at for j in starts_sorted]
    window = timedelta(hours=4)

    def _nearest(keys, rows, target):
        if not keys:
            return None
        i = bisect_left(keys, target)
        best, best_diff = None, window
        for ci in (i - 1, i):
            if 0 <= ci < len(rows):
                diff = abs(keys[ci] - target)
                if diff <= best_diff:
                    best_diff = diff
                    best = rows[ci]
        return best

    result = {}
    for cs in unfilled:
        j = _nearest(end_keys, ends_sorted, cs.started_at)
        coords = None
        if j is not None:
            coords = (j.end_lat, j.end_lng)
        else:
            ref = cs.ended_at or cs.started_at
            j = _nearest(start_keys, starts_sorted, ref)
            if j is not None:
                coords = (j.start_lat, j.start_lng)
        if coords is None:
            continue
        best_st, best_dist = None, MATCH_RADIUS_M
        for st in stations:
            if st.lat is None or st.lng is None:
                continue
            d = _haversine_m(coords[0], coords[1], st.lat, st.lng)
            if d <= best_dist:
                best_dist = d
                best_st = st
        if best_st is not None:
            result[cs.id] = {
                "station_id": best_st.id,
                "name": best_st.name,
                "price_ct_per_kwh": best_st.price_ct_per_kwh,
            }
    return result


def list_stations() -> list[ChargingStation]:
    with Session(get_engine()) as s:
        return list(s.exec(select(ChargingStation)).all())
