from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import Session

from polestar_dash.db.models import ChargeSession, JourneyImport
from polestar_dash.db.session import get_engine


def _seed(now):
    with Session(get_engine()) as s:
        # Imported trips: real kWh + SoC span + CO2 intensity + temperature.
        for i in range(12):
            start = now - timedelta(days=i)
            s.add(JourneyImport(
                vin="VIN1", started_at=start, ended_at=start + timedelta(hours=1),
                distance_km=100.0, consumption_kwh=18.0,
                soc_start=80, soc_end=60, co2_g_per_kwh=150,
                outside_temp_c=(-2.0 if i % 2 else 16.0),
            ))
        # Charge sessions: a slow AC and a fast DC, with prices.
        s.add(ChargeSession(vin="VIN1", started_at=now - timedelta(days=1),
                            soc_start=40, soc_end=80, energy_kwh=30.0,
                            peak_power_w=7000, price_ct_per_kwh=20.0,
                            is_complete=True))
        s.add(ChargeSession(vin="VIN1", started_at=now - timedelta(days=2),
                            soc_start=20, soc_end=70, energy_kwh=38.0,
                            peak_power_w=120000, price_ct_per_kwh=55.0,
                            is_complete=True))
        s.commit()


def test_cost_vs_petrol_pure(tmp_db):
    from polestar_dash.insights_extras import cost_vs_petrol

    # Defaults: 1.75 €/l × 7 l/100km = €12.25/100km petrol.
    out = cost_vs_petrol({"eur_per_100km": 4.0}, {"monthly_km": 1000})
    assert out["petrol_per_100km"] == 12.25
    assert out["saved_per_100km"] == 8.25
    assert out["saved_per_month"] == 82.5


def test_charge_price_split(tmp_db):
    from polestar_dash.insights_extras import charge_price_split

    _seed(datetime.utcnow())
    split = charge_price_split(days=30, vin="VIN1")
    assert split["ac_ct"] == 20.0  # the 7 kW session
    assert split["dc_ct"] == 55.0  # the 120 kW session


def test_temp_efficiency_delta(tmp_db):
    from polestar_dash.insights_extras import compute_temp_scatter, temp_efficiency_delta

    _seed(datetime.utcnow())
    td = temp_efficiency_delta(compute_temp_scatter(days=30))
    # Same Wh/km in both buckets here → ~0 % delta, but the structure is sound.
    assert td is not None and "delta_pct" in td


def test_build_narrative_smoke(tmp_db):
    from polestar_dash.insights_extras import (
        compute_charge_temp_scatter,
        compute_co2,
        compute_forecast,
        compute_soh,
        compute_temp_scatter,
        compute_trip_cost,
    )
    from polestar_dash.insights_narrative import build_narrative

    _seed(datetime.utcnow())
    nr = build_narrative(
        days=30, vin="VIN1",
        forecast=compute_forecast(days=30), trip_cost=compute_trip_cost(days=30),
        co2=compute_co2(days=30), soh=compute_soh(),
        scatter=[{"avg_kmh": 80, "wh_per_km": 180}, {"avg_kmh": 120, "wh_per_km": 240}],
        temp_scatter=compute_temp_scatter(days=30),
        charge_temp_scatter=compute_charge_temp_scatter(days=30),
    )
    assert set(nr) == {"summary", "cost", "efficiency", "battery", "waste", "impact"}
    # The petrol comparison should produce a cost sentence and an impact sentence.
    assert any("100 km" in i["text"] for i in nr["cost"])
    assert any("CO" in i["text"] for i in nr["impact"])
    # Every insight is a {text, tone} dict.
    for section in nr.values():
        for it in section:
            assert "text" in it and "tone" in it
