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
    own driven km/year, so the "current mix" curve reflects how *this*
    car is actually charged, not a generic assumption.

None of this is a warranty claim or a substitute for the car's own battery
health certificate — it's a rough, transparent model to compare charging
habits against each other on the same km axis.
"""

from __future__ import annotations

import math
from datetime import timedelta

from sqlmodel import Session, select

from .db.models import ChargeSession, JourneyImport
from .db.session import get_engine
from .sessions import DEFAULT_BATTERY_KWH
from .tripbook import detect_trips
from .util import utcnow

# AC onboard charger on a Polestar 2 tops out at 11 kW (16 A / phase, see
# cards.html). Anything reported above that had to come from a DC charger.
DC_THRESHOLD_W = 15_000

KM_MAX = 250_000
KM_STEP = 5_000

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

FLOOR_PCT = 55.0  # sanity clamp — never render below this, it's noise past here
DEFAULT_ANNUAL_KM = 15_000  # used only until enough driving history exists

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


def _annual_km() -> dict:
    """Best-effort driven km/year from journey imports, else detected trips."""
    since = utcnow() - timedelta(days=365)
    with Session(get_engine()) as s:
        imports = list(
            s.exec(select(JourneyImport).where(JourneyImport.started_at >= since)).all()
        )
    total_km = sum((j.distance_km or 0) for j in imports)
    days_covered = 365
    source = "imports"
    if total_km <= 0:
        trips = detect_trips(days=365)
        total_km = sum(t.km for t in trips)
        source = "detected"
    if total_km <= 0:
        return {"annual_km": DEFAULT_ANNUAL_KM, "source": "assumed"}
    return {"annual_km": round(total_km / days_covered * 365), "source": source}


def _soh_curve(km_grid: list[int], fast_frac: float, annual_km: int) -> list[float]:
    annual_pct = RATE_AC_PCT_PER_YEAR + (RATE_DC_PCT_PER_YEAR - RATE_AC_PCT_PER_YEAR) * fast_frac
    rate_pct_per_km = annual_pct / annual_km
    out = []
    for km in km_grid:
        early = EARLY_LOSS_PCT * math.sqrt(min(km, EARLY_KM) / EARLY_KM)
        linear = rate_pct_per_km * km
        out.append(round(max(FLOOR_PCT, 100 - early - linear), 1))
    return out


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
    for sc in scenarios.values():
        sc["soh"] = _soh_curve(km_grid, sc["fast_frac"], annual["annual_km"])

    return {
        "km_grid": km_grid,
        "scenarios": scenarios,
        "charge_mix": mix,
        "annual_km": annual["annual_km"],
        "annual_km_source": annual["source"],
        "nominal_kwh": DEFAULT_BATTERY_KWH,
        "warranty_km": WARRANTY_KM,
        "warranty_floor_pct": WARRANTY_FLOOR_PCT,
    }
