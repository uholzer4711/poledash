"""Aggregate repeated routes from imported journey-log trips.

Two trips count as the "same route" when their start- and end-places
match. A place is either:
  - a configured label (e.g. "Heim") if any label-pattern matches the
    geocoded address, or
  - a 1-km cluster of nearby endpoints (greedy clustering on lat/lng).

The 1-km tolerance avoids splitting visits to the same destination
across slightly different geocoded addresses (different street, side
of the building, GPS jitter). Trips with no coordinates fall back to
the first segment of the address.

The aggregator returns ordered tuples of (start, end), so
"Heim → Arbeit" and "Arbeit → Heim" are distinct.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from statistics import mean

from sqlmodel import Session, select

from .db.models import JourneyImport
from .db.session import get_engine
from .journey_import import geo_label_for, get_address_geofences, get_address_labels, label_for
from .util import haversine_km, utcnow


def _short(addr: str | None) -> str:
    if not addr:
        return "—"
    return addr.split(",")[0].strip() or "—"


def _cluster_places(
    points: list[tuple[float, float, str]],
    radius_km: float,
) -> dict[tuple[float, float], str]:
    """Greedy-cluster (lat, lng, short_addr) points within radius_km.

    Returns a mapping {(lat, lng): cluster_key}, where cluster_key is
    the most common short_addr among points in that cluster.
    """
    # cluster = (center_lat, center_lng, [short_addr, ...], [(lat, lng), ...])
    clusters: list[tuple[float, float, list[str], list[tuple[float, float]]]] = []
    for lat, lng, name in points:
        placed = False
        for i, (clat, clng, names, members) in enumerate(clusters):
            if haversine_km(lat, lng, clat, clng) <= radius_km:
                names.append(name)
                members.append((lat, lng))
                # recompute centroid so the cluster doesn't drift with insertion order
                nlat = sum(p[0] for p in members) / len(members)
                nlng = sum(p[1] for p in members) / len(members)
                clusters[i] = (nlat, nlng, names, members)
                placed = True
                break
        if not placed:
            clusters.append((lat, lng, [name], [(lat, lng)]))

    out: dict[tuple[float, float], str] = {}
    for _, _, names, members in clusters:
        key = Counter(n for n in names if n and n != "—").most_common(1)
        cluster_name = key[0][0] if key else "—"
        for lat, lng in members:
            out[(lat, lng)] = cluster_name
    return out


def _build_key_resolver(
    trips: list[JourneyImport],
    radius_km: float,
):
    """Return a function trip -> (start_key, end_key) for these trips.

    Endpoints with a matching address label get the label as their key.
    Endpoints with coordinates but no label are clustered together
    (start and end endpoints share one cluster space — going *to* a
    place and starting *from* the same place yield the same key).
    """
    labels = get_address_labels()
    geofences = get_address_geofences()

    def _label(addr: str | None, lat: float | None, lng: float | None) -> str | None:
        # String patterns first (cheap, exact intent), then radius geofences.
        return label_for(addr, labels) or geo_label_for(lat, lng, geofences)

    geo_points: list[tuple[float, float, str]] = []
    for t in trips:
        if not _label(t.start_address, t.start_lat, t.start_lng) and t.start_lat is not None and t.start_lng is not None:
            geo_points.append((t.start_lat, t.start_lng, _short(t.start_address)))
        if not _label(t.end_address, t.end_lat, t.end_lng) and t.end_lat is not None and t.end_lng is not None:
            geo_points.append((t.end_lat, t.end_lng, _short(t.end_address)))

    cluster_lookup = _cluster_places(geo_points, radius_km)

    def resolve(addr: str | None, lat: float | None, lng: float | None) -> str:
        lbl = _label(addr, lat, lng)
        if lbl:
            return lbl
        if lat is not None and lng is not None and (lat, lng) in cluster_lookup:
            return cluster_lookup[(lat, lng)]
        return _short(addr)

    def for_trip(t: JourneyImport) -> tuple[str, str]:
        return (
            resolve(t.start_address, t.start_lat, t.start_lng),
            resolve(t.end_address, t.end_lat, t.end_lng),
        )

    return for_trip


def list_routes(days: int = 365, min_trips: int = 2, radius_km: float = 1.0) -> list[dict]:
    since = utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        rows = list(
            s.exec(
                select(JourneyImport)
                .where(JourneyImport.started_at >= since)
                .order_by(JourneyImport.started_at.desc())
            ).all()
        )

    key_for = _build_key_resolver(rows, radius_km)

    buckets: dict[tuple[str, str], list[JourneyImport]] = {}
    for r in rows:
        buckets.setdefault(key_for(r), []).append(r)

    out: list[dict] = []
    for (start, end), trips in buckets.items():
        if len(trips) < min_trips:
            continue
        kms = [t.distance_km for t in trips if t.distance_km]
        kwhs = [t.consumption_kwh for t in trips if t.consumption_kwh and t.distance_km]
        durations = [
            (t.ended_at - t.started_at).total_seconds() / 60
            for t in trips
            if t.ended_at and t.started_at
        ]
        per100 = [
            t.consumption_kwh * 100 / t.distance_km
            for t in trips
            if t.consumption_kwh and t.distance_km and t.distance_km > 0.5
        ]
        temps = [t.outside_temp_c for t in trips if t.outside_temp_c is not None]
        pms = [t.air_pm25 for t in trips if t.air_pm25 is not None]
        out.append({
            "start": start,
            "end": end,
            "trips": len(trips),
            "total_km": round(sum(kms), 1) if kms else 0,
            "avg_km": round(mean(kms), 1) if kms else None,
            "total_kwh": round(sum(kwhs), 2) if kwhs else 0,
            "avg_kwh_per_100km": round(mean(per100), 1) if per100 else None,
            "best_kwh_per_100km": round(min(per100), 1) if per100 else None,
            "worst_kwh_per_100km": round(max(per100), 1) if per100 else None,
            "avg_minutes": round(mean(durations)) if durations else None,
            "min_minutes": round(min(durations)) if durations else None,
            "max_minutes": round(max(durations)) if durations else None,
            "avg_temp_c": round(mean(temps), 1) if temps else None,
            "avg_pm25": round(mean(pms), 1) if pms else None,
            "last_at": max((t.started_at for t in trips), default=None),
        })
    out.sort(key=lambda r: r["trips"], reverse=True)
    return out


def trips_for_route(
    start: str, end: str, days: int = 365, radius_km: float = 1.0
) -> list[JourneyImport]:
    since = utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        rows = list(
            s.exec(
                select(JourneyImport)
                .where(JourneyImport.started_at >= since)
                .order_by(JourneyImport.started_at.desc())
            ).all()
        )
    key_for = _build_key_resolver(rows, radius_km)
    return [r for r in rows if key_for(r) == (start, end)]
