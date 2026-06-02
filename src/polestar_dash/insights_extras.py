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


def compute_co2(days: int = 365) -> dict:
    """Tailpipe-equivalent grid CO₂ for imported trips in the window.

    Each trip's grams = consumption_kwh × co2_g_per_kwh (AT grid intensity at
    trip time). Returns total kg, g/km and a petrol-car comparison (a typical
    ICE emits ~150 g CO₂/km tank-to-wheel)."""
    since = utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        rows = list(
            s.exec(
                select(JourneyImport)
                .where(JourneyImport.started_at >= since)
                .where(JourneyImport.co2_g_per_kwh.is_not(None))
                .where(JourneyImport.consumption_kwh.is_not(None))
            ).all()
        )
    total_g = 0.0
    total_km = 0.0
    n = 0
    for r in rows:
        if not r.consumption_kwh:
            continue
        total_g += r.consumption_kwh * r.co2_g_per_kwh
        total_km += r.distance_km or 0
        n += 1
    ICE_G_PER_KM = 150.0  # typical petrol car, tank-to-wheel
    return {
        "window_days": days,
        "trips": n,
        "total_kg": round(total_g / 1000, 1),
        "total_km": round(total_km, 1),
        "g_per_km": round(total_g / total_km, 1) if total_km > 0 else None,
        "ice_kg": round(total_km * ICE_G_PER_KM / 1000, 1),
        "saved_kg": round((total_km * ICE_G_PER_KM - total_g) / 1000, 1),
    }


def compute_trip_cost(days: int = 365) -> dict:
    """Real energy cost of imported trips: consumption_kwh × blended price."""
    since = utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        rows = list(
            s.exec(
                select(JourneyImport)
                .where(JourneyImport.started_at >= since)
                .where(JourneyImport.consumption_kwh.is_not(None))
            ).all()
        )
    price_ct, price_source = blended_price_ct(days=max(days, 90))
    total_kwh = sum((r.consumption_kwh or 0) for r in rows)
    total_km = sum((r.distance_km or 0) for r in rows)
    total_eur = total_kwh * price_ct / 100.0
    return {
        "window_days": days,
        "trips": len(rows),
        "total_kwh": round(total_kwh, 1),
        "total_km": round(total_km, 1),
        "total_eur": round(total_eur, 2),
        "price_ct_per_kwh": price_ct,
        "price_source": price_source,
        "eur_per_100km": round(total_eur / total_km * 100, 2) if total_km > 0 else None,
        "ct_per_km": round(total_eur * 100 / total_km, 1) if total_km > 0 else None,
    }


def compute_battery_trend(min_soc_drop: int = 4, min_bucket_drop: int = 30) -> dict:
    """Implied usable battery capacity per month, to surface degradation.

    Same estimator as ``calibrate_battery_kwh`` (energy-weighted kWh-per-%×100)
    but bucketed per calendar month. A bucket is only emitted once its summed
    SoC drop reaches ``min_bucket_drop`` %, so a sparse month doesn't produce a
    noisy outlier. Needs many months of data to be meaningful — the caller
    shows a 'collecting data' hint until then."""
    with Session(get_engine()) as s:
        rows = list(s.exec(select(JourneyImport)).all())
    buckets: dict[str, list[tuple[float, int]]] = {}
    for j in rows:
        if not j.consumption_kwh or j.consumption_kwh <= 0:
            continue
        if j.soc_start is None or j.soc_end is None:
            continue
        drop = j.soc_start - j.soc_end
        if drop < min_soc_drop:
            continue
        buckets.setdefault(j.started_at.strftime("%Y-%m"), []).append((j.consumption_kwh, drop))
    points: list[dict] = []
    for month, pairs in sorted(buckets.items()):
        total_e = sum(e for e, _ in pairs)
        total_drop = sum(d for _, d in pairs)
        if total_drop < min_bucket_drop:
            continue
        points.append({
            "month": month,
            "kwh": round(total_e / total_drop * 100, 1),
            "samples": len(pairs),
        })
    return {"points": points, "enough_data": len(points) >= 3}


def compute_charge_temp_scatter(days: int = 365) -> list[dict]:
    """Peak charging power vs on-car temperature per completed session.

    Surfaces cold-charging behaviour (a cold pack throttles DC peak power).
    Only sessions stamped with a C3 temperature appear."""
    since = utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        rows = list(
            s.exec(
                select(ChargeSession)
                .where(ChargeSession.started_at >= since)
                .where(ChargeSession.is_complete.is_(True))  # noqa: E712
                .where(ChargeSession.outside_temp_c.is_not(None))
                .where(ChargeSession.peak_power_w.is_not(None))
            ).all()
        )
    out: list[dict] = []
    for r in rows:
        out.append({
            "temp_c": r.outside_temp_c,
            "peak_kw": round((r.peak_power_w or 0) / 1000, 1),
            "kwh": round(r.energy_kwh or 0, 1),
            "started_at": r.started_at.isoformat(),
        })
    return out


def compute_air_exposure(days: int = 365) -> dict:
    """Monthly average cabin PM2.5 from imported trips (C3-enriched).

    Returns {points:[{month, pm25, samples}], enough_data}."""
    since = utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        rows = list(
            s.exec(
                select(JourneyImport.started_at, JourneyImport.air_pm25)
                .where(JourneyImport.started_at >= since)
                .where(JourneyImport.air_pm25.is_not(None))
            ).all()
        )
    buckets: dict[str, list[float]] = {}
    for started_at, pm25 in rows:
        buckets.setdefault(started_at.strftime("%Y-%m"), []).append(pm25)
    points = [
        {"month": m, "pm25": round(sum(v) / len(v), 1), "samples": len(v)}
        for m, v in sorted(buckets.items())
    ]
    return {"points": points, "enough_data": len(points) >= 1}


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
