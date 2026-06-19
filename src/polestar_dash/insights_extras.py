"""Side computations for the /insights dashboard.

Each helper reads only what it needs and returns a JSON-friendly dict so
the existing /api/insights endpoint can splice them in without bloating
into one giant function.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlmodel import Session, select

from .app_settings import blended_price_ct
from .db.models import C3Snapshot, ChargeSession, JourneyImport, VehicleSnapshot
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
    from .app_settings import get_petrol_params

    ICE_G_PER_KM = float(get_petrol_params()["co2_g_per_km"])  # configurable ICE ref
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


def compute_soh(min_soc_span: int = 12, window: int = 12) -> dict:
    """Estimate battery State-of-Health (%).

    The Polestar/C3 API exposes **no** SoH or capacity field, so this is the
    TeslaMate-style estimate: every charge implies a usable pack capacity
    ``energy_kwh / (ΔSoC/100)`` and every month of driving the same from
    consumption. We take the median of the most recent ``window`` implied
    samples — each needs a SoC span ≥ ``min_soc_span`` % so the 1 %-resolution
    rounding noise stays small — and express it against the nominal new-pack
    capacity, capped at 100 %.

    Caveats (why this is an *estimate*, surfaced in the UI): AC charging losses
    inflate the charge-derived samples, so a healthy/near-new pack reads ~100 %;
    the indicator only drops once real degradation exceeds that bias. It does
    **not** need 100 % charges — just a decent SoC span per sample.
    """
    from .sessions import DEFAULT_BATTERY_KWH

    nominal = DEFAULT_BATTERY_KWH
    samples: list[tuple[datetime, float]] = []
    with Session(get_engine()) as s:
        charges = list(
            s.exec(
                select(ChargeSession)
                .where(ChargeSession.is_complete.is_(True))  # noqa: E712
                .where(ChargeSession.energy_kwh.is_not(None))
                .where(ChargeSession.soc_start.is_not(None))
                .where(ChargeSession.soc_end.is_not(None))
            ).all()
        )
    for c in charges:
        span = (c.soc_end or 0) - (c.soc_start or 0)
        if span >= min_soc_span and c.energy_kwh and c.energy_kwh > 0:
            samples.append((c.started_at, c.energy_kwh / (span / 100.0)))
    # Monthly driving aggregate (no charging losses → a cleaner second source).
    for pt in compute_battery_trend(min_soc_drop=4, min_bucket_drop=min_soc_span)["points"]:
        samples.append((datetime.strptime(pt["month"], "%Y-%m"), float(pt["kwh"])))

    # Drop implausible samples (bad ΔSoC / partial sessions).
    samples = [(t, k) for (t, k) in samples if 0.4 * nominal <= k <= 1.25 * nominal]
    if len(samples) < 3:
        return {
            "soh_pct": None, "implied_kwh": None, "nominal_kwh": round(nominal, 1),
            "samples": len(samples), "enough_data": False,
        }
    samples.sort(key=lambda x: x[0])
    recent = sorted(k for _, k in samples[-window:])
    n = len(recent)
    implied = recent[n // 2] if n % 2 else (recent[n // 2 - 1] + recent[n // 2]) / 2
    return {
        "soh_pct": min(100, round(implied / nominal * 100)),
        "implied_kwh": round(implied, 1),
        "nominal_kwh": round(nominal, 1),
        "samples": len(samples),
        "enough_data": True,
    }


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


def compute_c3_env_timeline(days: int = 30) -> dict:
    """Daily mean cabin PM2.5 + AQI and on-car outside temperature, straight
    from C3 snapshots.

    Independent of trip/session enrichment, so it carries data as soon as C3
    polling runs (the trip-level air/temp views need a drive *and* a matching
    C3 window first). Returns {points:[{date, pm25, aqi, temp_c, n}]}."""
    since = utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        rows = list(
            s.exec(
                select(
                    C3Snapshot.fetched_at,
                    C3Snapshot.air_pm25,
                    C3Snapshot.air_aqi,
                    C3Snapshot.weather_temp_c,
                ).where(C3Snapshot.fetched_at >= since)
            ).all()
        )

    buckets: dict[str, dict[str, list[float]]] = {}
    for fetched_at, pm25, aqi, temp in rows:
        b = buckets.setdefault(
            fetched_at.strftime("%Y-%m-%d"), {"pm": [], "aqi": [], "temp": []}
        )
        if pm25 is not None:
            b["pm"].append(pm25)
        if aqi is not None:
            b["aqi"].append(aqi)
        if temp is not None:
            b["temp"].append(temp)

    def _avg(xs: list[float], nd: int = 1) -> float | None:
        return round(sum(xs) / len(xs), nd) if xs else None

    points = [
        {
            "date": d,
            "pm25": _avg(b["pm"]),
            "aqi": _avg(b["aqi"], 0),
            "temp_c": _avg(b["temp"]),
            "n": max(len(b["pm"]), len(b["aqi"]), len(b["temp"])),
        }
        for d, b in sorted(buckets.items())
    ]
    return {"points": points}


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


# ---------------------------------------------------------------------------
# Insight helpers — small derived facts the narrative engine turns into
# plain-language sentences. Each is defensive: returns None / enough_data=False
# when there isn't enough history, so the UI can omit the sentence.
# ---------------------------------------------------------------------------

def _avg(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def temp_efficiency_delta(temp_scatter: list[dict]) -> dict | None:
    """How much more energy the car uses in the cold vs in mild weather.
    Buckets Wh/km by outside temp (<5 °C cold, 5–18 mild, >18 warm)."""
    cold = [p["wh_per_km"] for p in temp_scatter if p["temp_c"] < 5]
    mild = [p["wh_per_km"] for p in temp_scatter if 5 <= p["temp_c"] <= 18]
    warm = [p["wh_per_km"] for p in temp_scatter if p["temp_c"] > 18]
    cold_a, mild_a, warm_a = _avg(cold), _avg(mild), _avg(warm)
    base = mild_a if mild_a else warm_a
    if base is None or cold_a is None or len(cold) < 2 or (len(mild) + len(warm)) < 2:
        return None
    return {
        "cold_wh": round(cold_a), "mild_wh": round(base),
        "warm_wh": round(warm_a) if warm_a else None,
        "delta_pct": round((cold_a - base) / base * 100),
        "samples": len(cold) + len(mild) + len(warm),
    }


def speed_sweet_spot(scatter: list[dict]) -> dict | None:
    """The speed band with the lowest Wh/km, and how much a highway pace costs.
    `scatter` items carry `avg_kmh` and `wh_per_km` (from /api/insights)."""
    bands = [(0, 30), (30, 50), (50, 70), (70, 90), (90, 110), (110, 999)]
    agg: dict[tuple, list[float]] = {b: [] for b in bands}
    for p in scatter:
        kmh, wh = p.get("avg_kmh"), p.get("wh_per_km")
        if kmh is None or wh is None:
            continue
        for b in bands:
            if b[0] <= kmh < b[1]:
                agg[b].append(wh)
                break
    means = {b: _avg(v) for b, v in agg.items() if len(v) >= 2}
    if len(means) < 2:
        return None
    best = min(means, key=means.get)
    highway = means.get((90, 110)) or means.get((110, 999))
    best_wh = means[best]
    return {
        "best_low": best[0], "best_high": best[1] if best[1] < 999 else None,
        "best_wh": round(best_wh),
        "highway_wh": round(highway) if highway else None,
        "highway_delta_pct": (
            round((highway - best_wh) / best_wh * 100) if highway and best_wh else None
        ),
        "samples": sum(len(v) for v in agg.values()),
    }


def range_now_vs_then(days: int = 180) -> dict | None:
    """Implied 100 %-pack range now vs ~`days` ago, from compute_range_factor."""
    pts = compute_range_factor(days=days)
    if len(pts) < 6:
        return None
    recent = [p["range_at_100"] for p in pts[-7:]]
    early = [p["range_at_100"] for p in pts[:7]]
    now_a, then_a = _avg(recent), _avg(early)
    if not now_a or not then_a:
        return None
    return {
        "now_km": round(now_a), "then_km": round(then_a),
        "delta_pct": round((now_a - then_a) / then_a * 100),
        "span_days": days,
    }


def cold_charge_throttle(charge_temp_scatter: list[dict]) -> dict | None:
    """Peak charging power when cold (<5 °C) vs warm (>15 °C)."""
    cold = [p["peak_kw"] for p in charge_temp_scatter if p["temp_c"] < 5]
    warm = [p["peak_kw"] for p in charge_temp_scatter if p["temp_c"] > 15]
    cold_a, warm_a = _avg(cold), _avg(warm)
    if cold_a is None or warm_a is None or not warm_a:
        return None
    return {
        "cold_kw": round(cold_a, 1), "warm_kw": round(warm_a, 1),
        "drop_pct": round((warm_a - cold_a) / warm_a * 100),
        "samples": len(cold) + len(warm),
    }


def cost_vs_petrol(trip_cost: dict, forecast: dict) -> dict | None:
    """EV €/100 km vs an equivalent petrol car + projected monthly saving."""
    from .app_settings import get_petrol_params

    ev_per100 = trip_cost.get("eur_per_100km")
    if ev_per100 is None:
        return None
    p = get_petrol_params()
    petrol_per100 = p["price_eur_per_l"] * p["l_per_100km"]
    monthly_km = forecast.get("monthly_km") or 0
    return {
        "ev_per_100km": round(ev_per100, 2),
        "petrol_per_100km": round(petrol_per100, 2),
        "saved_per_100km": round(petrol_per100 - ev_per100, 2),
        "saved_per_month": round((petrol_per100 - ev_per100) * monthly_km / 100, 2),
        "monthly_km": round(monthly_km),
    }


def _eur_per_100km_window(start, end, vin: str | None) -> float | None:  # noqa: ANN001
    """EV €/100 km for an explicit [start, end) window (imported trips)."""
    price_ct, _ = blended_price_ct(days=90)
    with Session(get_engine()) as s:
        stmt = (
            select(JourneyImport)
            .where(JourneyImport.started_at >= start)
            .where(JourneyImport.started_at < end)
            .where(JourneyImport.consumption_kwh.is_not(None))
        )
        if vin:
            stmt = stmt.where(JourneyImport.vin == vin)
        rows = list(s.exec(stmt).all())
    kwh = sum((r.consumption_kwh or 0) for r in rows)
    km = sum((r.distance_km or 0) for r in rows)
    if km <= 0:
        return None
    return round(kwh * price_ct / 100.0 / km * 100, 2)


def month_over_month_cost(vin: str | None = None) -> dict | None:
    """€/100 km of the last 30 days vs the prior 30 days."""
    now = utcnow()
    cur = _eur_per_100km_window(now - timedelta(days=30), now, vin)
    prev = _eur_per_100km_window(now - timedelta(days=60), now - timedelta(days=30), vin)
    if cur is None or prev is None or prev == 0:
        return None
    return {"current": cur, "previous": prev, "delta_pct": round((cur - prev) / prev * 100)}


def charge_price_split(days: int = 365, vin: str | None = None) -> dict | None:
    """Average ct/kWh for slow AC (peak < 11 kW) vs fast DC (peak ≥ 22 kW)."""
    from .app_settings import get_default_price_ct

    default_ct = get_default_price_ct()
    since = utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        stmt = (
            select(ChargeSession)
            .where(ChargeSession.started_at >= since)
            .where(ChargeSession.is_complete.is_(True))  # noqa: E712
            .where(ChargeSession.energy_kwh.is_not(None))
            .where(ChargeSession.peak_power_w.is_not(None))
        )
        if vin:
            stmt = stmt.where(ChargeSession.vin == vin)
        rows = list(s.exec(stmt).all())
    ac = {"kwh": 0.0, "cost": 0.0}
    dc = {"kwh": 0.0, "cost": 0.0}
    for r in rows:
        kwh = r.energy_kwh or 0
        if kwh <= 0:
            continue
        ct = r.price_ct_per_kwh if r.price_ct_per_kwh is not None else default_ct
        bucket = ac if (r.peak_power_w or 0) < 11000 else (dc if (r.peak_power_w or 0) >= 22000 else None)
        if bucket is None:
            continue
        bucket["kwh"] += kwh
        bucket["cost"] += kwh * ct
    out = {}
    if ac["kwh"] > 0:
        out["ac_ct"] = round(ac["cost"] / ac["kwh"], 1)
        out["ac_kwh"] = round(ac["kwh"], 1)
    if dc["kwh"] > 0:
        out["dc_ct"] = round(dc["cost"] / dc["kwh"], 1)
        out["dc_kwh"] = round(dc["kwh"], 1)
    return out or None


# NOTE: the old `idle_loss_summary` (kWh/€ across sparse, weakly-attributed
# discrete parked draws) was removed in favour of `idle_drain.standby_baseline`
# — an aggregate %/day parasitic-standby rate that doesn't hang the headline on
# a single noisy event. See idle_drain.py.
