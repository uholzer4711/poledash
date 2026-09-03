"""Long-range battery-degradation projection (0–250,000 km).

The Polestar/C3 API reports no cell chemistry or degradation model, so this
is an *illustrative* estimate, not a spec. It combines:

  * The pack this app is configured for: 78 kWh Long Range NMC (CATL/LG
    pouch cells, liquid-cooled) — see ``sessions.DEFAULT_BATTERY_KWH``.
    Polestar's warranty floor is 70 % SoH within 8 years / 160,000 km.
  * Published fleet-degradation studies (Geotab's ~10k-EV analysis; NMC
    fast-charge-cost literature): liquid-cooled NMC packs run roughly
    1.3 %/year under AC-only charging up to ~2.8 %/year under heavy DC
    fast-charging, plus an early SEI/formation loss (~2.5 % in the first
    ~30,000 km) independent of charging habits.
  * This car's own measured charge mix (``ChargeSession.peak_power_w``
    above the Polestar 2's 11 kW AC onboard-charger ceiling ⇒ DC) and its
    own driven km/year — from the **odometer**, not journey imports, since
    imports are a manually-curated subset and dividing their total by a
    hardcoded 365 days silently produced a wildly-too-low annual mileage
    (and thus a wildly-too-steep curve) whenever imports didn't cover the
    full year. The odometer is polled continuously and has no such gap.

None of this is a warranty claim or a substitute for the car's own battery
health certificate — it's a rough, transparent model to compare charging
habits against each other on the same km axis.
"""

from __future__ import annotations

import math
from datetime import timedelta

from sqlmodel import Session, select

from .db.models import ChargeSession, JourneyImport, VehicleSnapshot
from .db.session import get_engine
from .sessions import DEFAULT_BATTERY_KWH
from .tripbook import detect_trips
from .util import utcnow

# The Polestar 2's onboard charger hard-caps AC draw at 11 kW (16 A / phase,
# see cards.html) regardless of what the charge point can supply — so, unlike
# the 11-22 kW "could be either" zone treated as ambiguous elsewhere in this
# app (multi-vehicle cost bucketing in insights_extras.py), a Polestar 2 can
# physically never pull more than ~11.2 kW (measurement rounding) over AC.
# Real data confirms this: every AC session on this account reads 11.0-11.2 kW,
# every DC session 49 kW+, nothing in between. 15 kW sits safely in that gap.
DC_THRESHOLD_W = 15_000

KM_MAX = 250_000
KM_STEP = 10_000

# Early-life SEI/formation loss: chemistry-driven, not charging-mix-driven,
# so it applies identically to every scenario. Modeled as sqrt(km) so it
# front-loads (matches "a new pack drops a few % in year one, then flattens").
EARLY_LOSS_PCT = 2.5
EARLY_KM = 30_000

# Annual %-loss anchors for the *linear* cycling term, interpolated by DC
# fast-charge energy fraction (0 = AC-only, 1 = DC-only). Sourced from
# Geotab's fleet study (1.5 %/yr low-power-primary vs 3.0 %/yr heavy DC>100kW)
# adjusted down slightly for the Polestar 2's liquid-cooled pack, which fares
# better than the air-cooled packs pulling that fleet average up.
RATE_AC_PCT_PER_YEAR = 1.3
RATE_DC_PCT_PER_YEAR = 2.8

FLOOR_PCT = 50.0  # sanity clamp — never render below this, it's noise past here
DEFAULT_ANNUAL_KM = 15_000  # used only until enough driving history exists
MIN_SPAN_DAYS = 14  # below this, an odometer/import extrapolation is too noisy
ANNUAL_KM_MIN, ANNUAL_KM_MAX = 1_000, 60_000  # sanity clamp on the extrapolation itself

WARRANTY_KM = 160_000
WARRANTY_FLOOR_PCT = 70.0


def _charge_mix() -> dict:
    """DC-fast vs AC-slow energy fraction from this car's own charge history."""
    with Session(get_engine()) as s:
        rows = list(
            s.exec(
                select(ChargeSession.peak_power_w, ChargeSession.energy_kwh)
                .where(ChargeSession.is_complete.is_(True))  # noqa: E712
                .where(ChargeSession.energy_kwh.is_not(None))
                .where(ChargeSession.peak_power_w.is_not(None))
            ).all()
        )
    fast_kwh = sum(kwh for pw, kwh in rows if pw and pw > DC_THRESHOLD_W)
    slow_kwh = sum(kwh for pw, kwh in rows if pw and pw <= DC_THRESHOLD_W)
    total = fast_kwh + slow_kwh
    return {
        "sessions": len(rows),
        "fast_kwh": round(fast_kwh, 1),
        "slow_kwh": round(slow_kwh, 1),
        "fast_frac": (fast_kwh / total) if total > 0 else None,
    }


def _extrapolate_annual(km_driven: float, days: float) -> int | None:
    if days < MIN_SPAN_DAYS or km_driven <= 0:
        return None
    annual = km_driven / days * 365
    return round(min(ANNUAL_KM_MAX, max(ANNUAL_KM_MIN, annual)))


def _annual_km() -> dict:
    """Driven km/year, preferring the continuously-polled odometer.

    Journey imports and detected trips are a curated/partial subset of the
    car's actual driving, so dividing their total by a fixed 365 days
    understates annual mileage whenever they don't cover the full year —
    the odometer has no such gap and is always the better source when it
    spans enough time to extrapolate from.
    """
    since = utcnow() - timedelta(days=365)
    with Session(get_engine()) as s:
        stmt = (
            select(VehicleSnapshot.fetched_at, VehicleSnapshot.odometer_km)
            .where(VehicleSnapshot.fetched_at >= since)
            .where(VehicleSnapshot.odometer_km.is_not(None))
            .order_by(VehicleSnapshot.fetched_at)
        )
        rows = list(s.exec(stmt).all())
    if len(rows) >= 2:
        first_ts, first_odo = rows[0]
        last_ts, last_odo = rows[-1]
        days = (last_ts - first_ts).total_seconds() / 86400
        annual = _extrapolate_annual((last_odo or 0) - (first_odo or 0), days)
        if annual is not None:
            return {"annual_km": annual, "source": "odometer"}

    with Session(get_engine()) as s:
        imports = list(
            s.exec(select(JourneyImport).where(JourneyImport.started_at >= since)).all()
        )
    if imports:
        total_km = sum((j.distance_km or 0) for j in imports)
        days = (utcnow() - min(j.started_at for j in imports)).total_seconds() / 86400
        annual = _extrapolate_annual(total_km, days)
        if annual is not None:
            return {"annual_km": annual, "source": "imports"}

    trips = detect_trips(days=365)
    if trips:
        total_km = sum(t.km for t in trips)
        days = (utcnow() - min(t.started_at for t in trips)).total_seconds() / 86400
        annual = _extrapolate_annual(total_km, days)
        if annual is not None:
            return {"annual_km": annual, "source": "detected"}

    return {"annual_km": DEFAULT_ANNUAL_KM, "source": "assumed"}


def _current_odometer_km() -> float | None:
    with Session(get_engine()) as s:
        stmt = (
            select(VehicleSnapshot.odometer_km)
            .where(VehicleSnapshot.odometer_km.is_not(None))
            .order_by(VehicleSnapshot.fetched_at.desc())
            .limit(1)
        )
        return s.exec(stmt).first()


def _rate_pct_per_km(fast_frac: float, annual_km: int) -> float:
    annual_pct = RATE_AC_PCT_PER_YEAR + (RATE_DC_PCT_PER_YEAR - RATE_AC_PCT_PER_YEAR) * fast_frac
    return annual_pct / annual_km


def _soh_at_rate(km: int, rate_pct_per_km: float) -> float:
    early = EARLY_LOSS_PCT * math.sqrt(min(km, EARLY_KM) / EARLY_KM)
    return round(max(FLOOR_PCT, 100 - early - rate_pct_per_km * km), 1)


def _soh_curve(km_grid: list[int], fast_frac: float, annual_km: int) -> list[float]:
    rate = _rate_pct_per_km(fast_frac, annual_km)
    return [_soh_at_rate(km, rate) for km in km_grid]


def _soh_at(km: int, fast_frac: float, annual_km: int) -> float:
    return _soh_at_rate(km, _rate_pct_per_km(fast_frac, annual_km))


def compute_degradation_forecast() -> dict:
    km_grid = list(range(0, KM_MAX + 1, KM_STEP))
    mix = _charge_mix()
    annual = _annual_km()
    current_frac = mix["fast_frac"] if mix["fast_frac"] is not None else 0.3

    scenarios = {
        "current": {"fast_frac": round(current_frac, 3), "measured": mix["fast_frac"] is not None},
        "heavy_fast": {"fast_frac": 0.85, "measured": None},
        "heavy_slow": {"fast_frac": 0.05, "measured": None},
    }
    checkpoints = [100_000, WARRANTY_KM, 250_000]
    for sc in scenarios.values():
        sc["soh"] = _soh_curve(km_grid, sc["fast_frac"], annual["annual_km"])
        sc["at"] = {km: _soh_at(km, sc["fast_frac"], annual["annual_km"]) for km in checkpoints}

    odo = _current_odometer_km()
    from .insights_extras import compute_soh

    soh_now = compute_soh()
    today = None
    if odo is not None and soh_now.get("enough_data"):
        today = {"km": round(odo), "soh_pct": soh_now["soh_pct"]}

    return {
        "km_grid": km_grid,
        "scenarios": scenarios,
        "charge_mix": mix,
        "annual_km": annual["annual_km"],
        "annual_km_source": annual["source"],
        "nominal_kwh": DEFAULT_BATTERY_KWH,
        "warranty_km": WARRANTY_KM,
        "warranty_floor_pct": WARRANTY_FLOOR_PCT,
        "today": today,
    }
