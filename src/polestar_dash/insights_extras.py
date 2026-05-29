"""Side computations for the /insights dashboard.

Each helper reads only what it needs and returns a JSON-friendly dict so
the existing /api/insights endpoint can splice them in without bloating
into one giant function.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlmodel import Session, select

from .app_settings import blended_price_ct
from .db.models import ChargeSession, JourneyImport, VehicleSnapshot
from .db.session import get_engine
from .util import utcnow


def compute_forecast(days: int = 30) -> dict:
    """Project monthly km / kWh / cost from the rolling-`days` window.

    Prefers imported journey-log trips (real kWh). Falls back to detected
    trips' SoC-estimated kWh when none are imported in the window.
    """
    since = utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        imports = list(
            s.exec(
                select(JourneyImport).where(JourneyImport.started_at >= since)
            ).all()
        )
    total_km = sum((j.distance_km or 0) for j in imports)
    total_kwh = sum((j.consumption_kwh or 0) for j in imports)
    source = "imports"
    if total_km <= 0:
        # Fall back to detected snapshots when we have no imports.
        from .tripbook import detect_trips

        trips = detect_trips(days=days)
        total_km = sum(t.km for t in trips)
        total_kwh = sum((t.kwh_used or 0) for t in trips)
        source = "detected"
    price_ct, price_source = blended_price_ct(days=max(days, 90))
    km_per_day = total_km / days if days > 0 else 0
    kwh_per_day = total_kwh / days if days > 0 else 0
    monthly_km = km_per_day * 30
    monthly_kwh = kwh_per_day * 30
    monthly_cost = monthly_kwh * price_ct / 100.0
    return {
        "window_days": days,
        "source": source,
        "km_per_day": round(km_per_day, 1),
        "monthly_km": round(monthly_km, 0),
        "monthly_kwh": round(monthly_kwh, 1),
        "price_ct_per_kwh": price_ct,
        "price_source": price_source,
        "monthly_cost_eur": round(monthly_cost, 2),
        "avg_kwh_per_100km": (
            round(total_kwh * 100 / total_km, 1) if total_km > 0 else None
        ),
    }


def compute_range_factor(days: int = 180) -> list[dict]:
    """Implied 100 %-pack range from snapshots, bucketed per day.

    range_at_100 = range_km / (soc_percent / 100). Larger = more optimistic
    onboard estimate. Smoothing happens client-side; we just return the
    daily mean."""
    since = utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        stmt = (
            select(VehicleSnapshot.fetched_at, VehicleSnapshot.range_km, VehicleSnapshot.soc_percent)
            .where(VehicleSnapshot.fetched_at >= since)
            .where(VehicleSnapshot.range_km.is_not(None))
            .where(VehicleSnapshot.soc_percent.is_not(None))
        )
        rows = list(s.exec(stmt).all())

    buckets: dict[str, list[float]] = {}
    for ts, range_km, soc in rows:
        if not soc or not range_km:
            continue
        full = range_km / (soc / 100)
        buckets.setdefault(ts.date().isoformat(), []).append(full)
    return [
        {"date": d, "range_at_100": round(sum(vs) / len(vs)), "samples": len(vs)}
        for d, vs in sorted(buckets.items())
    ]


def compute_charge_heatmap(days: int = 365) -> dict:
    """Per-day kWh charged for the last `days` days.

    Returns a dict keyed by ISO date (only days with kWh > 0); the caller
    fills the gaps when rendering the grid. Plus a max for color scaling.
    """
    since_d = (utcnow() - timedelta(days=days)).date()
    with Session(get_engine()) as s:
        rows = list(
            s.exec(
                select(ChargeSession)
                .where(ChargeSession.is_complete.is_(True))  # noqa: E712
                .where(ChargeSession.started_at >= datetime.combine(since_d, datetime.min.time()))
            ).all()
        )
    by_day: dict[str, float] = {}
    for r in rows:
        if not r.energy_kwh:
            continue
        d = r.started_at.date().isoformat()
        by_day[d] = round(by_day.get(d, 0) + r.energy_kwh, 2)
    return {
        "since": since_d.isoformat(),
        "until": date.today().isoformat(),
        "by_day": by_day,
        "max_kwh": max(by_day.values(), default=0),
    }


def compute_temp_scatter(days: int = 365) -> list[dict]:
    """Wh/km vs outside temperature per imported trip with weather data."""
    since = utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        rows = list(
            s.exec(
                select(JourneyImport)
                .where(JourneyImport.started_at >= since)
                .where(JourneyImport.outside_temp_c.is_not(None))
                .where(JourneyImport.distance_km.is_not(None))
                .where(JourneyImport.consumption_kwh.is_not(None))
            ).all()
        )
    out: list[dict] = []
    for r in rows:
        if not r.distance_km or r.distance_km < 1:
            continue
        out.append({
            "temp_c": r.outside_temp_c,
            "wh_per_km": round(r.consumption_kwh * 1000 / r.distance_km),
            "km": r.distance_km,
            "started_at": r.started_at.isoformat(),
        })
    return out
