"""Prometheus-format /metrics endpoint."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest
from sqlmodel import Session, select

from .db.models import C3Snapshot, VehicleSnapshot
from .db.session import get_engine

_OPEN_STATES = {"open", "ajar"}


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

    _render_c3(reg)
    return generate_latest(reg), CONTENT_TYPE_LATEST


_C3_OPENINGS = [
    "door_fl", "door_fr", "door_rl", "door_rr",
    "win_fl", "win_fr", "win_rl", "win_rr",
    "sunroof", "hood", "tailgate", "tank_lid",
]


def _render_c3(reg: CollectorRegistry) -> None:
    """C3 gauges from the latest C3Snapshot per VIN — lock/openings, climate,
    cabin air, on-car weather, location, and the C3-side battery view.
    Best-effort: a missing/empty C3Snapshot table just yields no samples."""
    g = lambda name, desc, labels=("vin",): Gauge(name, desc, list(labels), registry=reg)  # noqa: E731

    g_locked = g("poledash_c3_locked", "1 if locked, 0 if unlocked")
    g_any_door = g("poledash_c3_any_door_open", "1 if any door is open")
    g_opening = g("poledash_c3_opening_open", "1 if this opening is open/ajar", ("vin", "part"))
    g_openings = g("poledash_c3_openings_open", "Count of open doors/windows/openings")
    g_tyre = g("poledash_c3_tyre_warning", "1 if a tyre pressure warning is active")
    g_light = g("poledash_c3_light_warning", "1 if a bulb/light failure is active")
    g_climate = g("poledash_c3_climate_active", "1 if climate active")
    g_climate_rem = g("poledash_c3_climate_remaining_min", "Climate time remaining (min)")
    g_air_run = g("poledash_c3_air_precleaning", "1 if cabin pre-cleaning running")
    g_pm25 = g("poledash_c3_cabin_pm25", "Cabin PM2.5 (µg/m³)")
    g_aqi = g("poledash_c3_cabin_aqi", "Cabin air quality index")
    g_temp = g("poledash_c3_outside_temp_c", "On-car outside temperature (°C)")
    g_speed = g("poledash_c3_speed", "Current speed (C3 units)")
    g_heading = g("poledash_c3_heading", "Heading (degrees)")
    g_lat = g("poledash_c3_lat", "Latitude (degrees)")
    g_lng = g("poledash_c3_lng", "Longitude (degrees)")
    g_soc = g("poledash_c3_charge_level", "C3 state of charge (%)")
    g_range = g("poledash_c3_range_km", "C3 estimated range (km)")
    g_charging = g("poledash_c3_is_charging", "1 if C3 reports charging")
    g_plugged = g("poledash_c3_is_plugged_in", "1 if plugged in")
    g_amp = g("poledash_c3_amp_limit", "Charging amperage limit (A)")

    def b(v: bool | None) -> int | None:
        return None if v is None else (1 if v else 0)

    with Session(get_engine()) as s:
        vins = list(s.exec(select(C3Snapshot.vin).distinct()).all())
        for vin in vins:
            row = s.exec(
                select(C3Snapshot)
                .where(C3Snapshot.vin == vin)
                .order_by(C3Snapshot.fetched_at.desc())
                .limit(1)
            ).first()
            if row is None:
                continue
            pairs = [
                (g_locked, b(row.is_locked)),
                (g_any_door, b(row.any_door_open)),
                (g_tyre, b(row.any_tyre_warning)),
                (g_light, b(row.any_light_failure)),
                (g_climate, b(row.climate_active)),
                (g_climate_rem, row.climate_time_remaining),
                (g_air_run, b(row.air_running)),
                (g_pm25, row.air_pm25),
                (g_aqi, row.air_aqi),
                (g_temp, row.weather_temp_c),
                (g_speed, row.speed),
                (g_heading, row.heading),
                (g_lat, row.lat),
                (g_lng, row.lng),
                (g_soc, row.charge_level),
                (g_range, row.range_km),
                (g_charging, b(row.is_charging)),
                (g_plugged, b(row.is_plugged_in)),
                (g_amp, row.amp_limit),
            ]
            for gauge, val in pairs:
                if val is not None:
                    gauge.labels(vin).set(val)
            g_openings.labels(vin).set(
                sum(1 for a in _C3_OPENINGS if getattr(row, a) in _OPEN_STATES)
            )
            for part in _C3_OPENINGS:
                val = getattr(row, part)
                if val not in (None, "—", "?"):
                    g_opening.labels(vin, part).set(1 if val in _OPEN_STATES else 0)
