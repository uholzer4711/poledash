"""Prometheus-format /metrics endpoint."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest
from sqlmodel import Session, select

from .db.models import VehicleSnapshot
from .db.session import get_engine


def render_metrics() -> tuple[bytes, str]:
    """Generate a Prometheus exposition snapshot of the latest sample per VIN."""
    reg = CollectorRegistry()

    g_soc = Gauge("poledash_soc_percent", "State of charge in %", ["vin"], registry=reg)
    g_range = Gauge("poledash_range_km", "Estimated range in km", ["vin"], registry=reg)
    g_power = Gauge("poledash_charging_power_w", "Charging power in W", ["vin"], registry=reg)
    g_current = Gauge("poledash_charging_current_a", "Charging current in A", ["vin"], registry=reg)
    g_min2full = Gauge("poledash_minutes_to_full", "Minutes to full", ["vin"], registry=reg)
    g_odo = Gauge("poledash_odometer_km", "Odometer in km", ["vin"], registry=reg)
    g_cons = Gauge(
        "poledash_avg_consumption_kwh_per_100km",
        "Average consumption (kWh/100km)",
        ["vin"],
        registry=reg,
    )
    g_days_serv = Gauge("poledash_days_to_service", "Days to next service", ["vin"], registry=reg)
    g_km_serv = Gauge(
        "poledash_distance_to_service_km", "Distance to next service (km)", ["vin"], registry=reg
    )
    g_charging = Gauge(
        "poledash_charging",
        "1 if currently charging, else 0",
        ["vin"],
        registry=reg,
    )
    g_voltage = Gauge(
        "poledash_charging_voltage_v", "Charging voltage in V", ["vin"], registry=reg
    )
    g_target_soc = Gauge(
        "poledash_target_soc_percent",
        "Configured charge limit (target SoC) in %",
        ["vin"],
        registry=reg,
    )
    g_min2min = Gauge(
        "poledash_minutes_to_minimum_soc",
        "Minutes to configured minimum SoC",
        ["vin"],
        registry=reg,
    )
    g_charging_ac = Gauge(
        "poledash_charging_ac",
        "1 if currently charging on AC, 0 otherwise",
        ["vin"],
        registry=reg,
    )
    g_charging_dc = Gauge(
        "poledash_charging_dc",
        "1 if currently charging on DC, 0 otherwise",
        ["vin"],
        registry=reg,
    )

    with Session(get_engine()) as s:
        # latest snapshot per VIN
        vins = list(s.exec(select(VehicleSnapshot.vin).distinct()).all())
        for vin in vins:
            row = s.exec(
                select(VehicleSnapshot)
                .where(VehicleSnapshot.vin == vin)
                .order_by(VehicleSnapshot.fetched_at.desc())
                .limit(1)
            ).first()
            if row is None:
                continue
            if row.soc_percent is not None:
                g_soc.labels(vin).set(row.soc_percent)
            if row.range_km is not None:
                g_range.labels(vin).set(row.range_km)
            if row.charging_power_w is not None:
                g_power.labels(vin).set(row.charging_power_w)
            if row.charging_current_a is not None:
                g_current.labels(vin).set(row.charging_current_a)
            if row.minutes_to_full is not None:
                g_min2full.labels(vin).set(row.minutes_to_full)
            if row.odometer_km is not None:
                g_odo.labels(vin).set(row.odometer_km)
            if row.avg_consumption_kwh_per_100km is not None:
                g_cons.labels(vin).set(row.avg_consumption_kwh_per_100km)
            if row.days_to_service is not None:
                g_days_serv.labels(vin).set(row.days_to_service)
            if row.distance_to_service_km is not None:
                g_km_serv.labels(vin).set(row.distance_to_service_km)
            charging = row.charging_status in ("Charging", "Smart Charging")
            g_charging.labels(vin).set(1 if charging else 0)
            if row.charging_voltage_v is not None:
                g_voltage.labels(vin).set(row.charging_voltage_v)
            if row.target_soc_percent is not None:
                g_target_soc.labels(vin).set(row.target_soc_percent)
            if row.minutes_to_minimum_soc is not None:
                g_min2min.labels(vin).set(row.minutes_to_minimum_soc)
            g_charging_ac.labels(vin).set(1 if charging and row.charging_type == "AC" else 0)
            g_charging_dc.labels(vin).set(1 if charging and row.charging_type == "DC" else 0)

    return generate_latest(reg), CONTENT_TYPE_LATEST
