"""Aggregate repeated routes from imported journey-log trips.

Two trips count as the "same route" when their labeled start- and
end-addresses match (e.g. "Heim → Arbeit"). Unlabeled trips are grouped
by the first segment of their geocoded address — coarse but usually
sufficient for finding patterns. The aggregator returns ordered tuples
of (start, end), so "Heim → Arbeit" and "Arbeit → Heim" are distinct.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import mean
from typing import Iterable

from sqlmodel import Session, select

from .db.models import JourneyImport
from .db.session import get_engine
from .journey_import import label_for


def _place(addr: str | None) -> str:
    if not addr:
        return "—"
    lbl = label_for(addr)
    if lbl:
        return lbl
    return addr.split(",")[0]


def list_routes(days: int = 365, min_trips: int = 2) -> list[dict]:
    since = datetime.utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        rows = list(
            s.exec(
                select(JourneyImport)
                .where(JourneyImport.started_at >= since)
                .order_by(JourneyImport.started_at.desc())
            ).all()
        )

    buckets: dict[tuple[str, str], list[JourneyImport]] = {}
    for r in rows:
        key = (_place(r.start_address), _place(r.end_address))
        buckets.setdefault(key, []).append(r)

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
            "last_at": max((t.started_at for t in trips), default=None),
        })
    out.sort(key=lambda r: r["trips"], reverse=True)
    return out


def trips_for_route(start: str, end: str, days: int = 365) -> list[JourneyImport]:
    since = datetime.utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        rows = list(
            s.exec(
                select(JourneyImport)
                .where(JourneyImport.started_at >= since)
                .order_by(JourneyImport.started_at.desc())
            ).all()
        )
    return [r for r in rows if _place(r.start_address) == start and _place(r.end_address) == end]
