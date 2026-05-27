from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Index
from sqlmodel import JSON, Column, Field, SQLModel

from ..util import utcnow


class VehicleSnapshot(SQLModel, table=True):
    """Structured snapshot — one row per successful poll."""

    id: int | None = Field(default=None, primary_key=True)
    fetched_at: datetime = Field(index=True)
    vin: str = Field(index=True)

    model_name: str | None = None
    model_year: str | None = None
    registration_no: str | None = None
    software_version: str | None = None
    battery_spec: str | None = None
    torque: str | None = None
    image_url: str | None = None
    # Structured vehicle info (extracted from CarInformationData)
    internal_vehicle_id: str | None = None
    registration_date: str | None = None  # ISO date string from API
    factory_complete_date: str | None = None
    software_version_ts: datetime | None = None
    # Battery spec (structured) — preferred over the SoC×constant estimate
    battery_voltage_v: float | None = None
    battery_capacity_kwh: float | None = None
    battery_modules: int | None = None
    battery_cells: int | None = None
    # Pending target-SoC change queued in the Polestar app
    pending_target_soc_percent: int | None = None
    pending_target_soc_type: str | None = None

    soc_percent: int | None = None
    range_km: int | None = None
    range_miles: int | None = None
    avg_consumption_kwh_per_100km: float | None = None
    avg_consumption_kwh_per_100km_automatic: float | None = None
    avg_consumption_kwh_per_100km_since_charge: float | None = None
    total_energy_consumption_wh: float | None = None
    total_energy_consumption_wh_automatic: float | None = None
    total_energy_consumption_wh_since_charge: float | None = None
    charger_power_status_raw: int | None = None
    charging_status: str | None = None
    charger_connection_status: str | None = None
    charging_power_w: int | None = None
    charging_current_a: int | None = None
    minutes_to_full: int | None = None
    minutes_to_target: int | None = None
    minutes_to_minimum_soc: int | None = None
    charging_voltage_v: int | None = None
    charging_type: str | None = None  # "AC", "DC", "Unspecified"
    target_soc_percent: int | None = None
    target_soc_type: str | None = None  # "Daily", "Long Trip", "Custom"
    battery_event_ts: datetime | None = None

    odometer_km: float | None = None
    avg_speed_kmh: int | None = None
    trip_auto_km: float | None = None
    trip_manual_km: float | None = None
    odometer_event_ts: datetime | None = None

    service_warning: str | None = None
    days_to_service: int | None = None
    distance_to_service_km: int | None = None
    oil_level_warning: str | None = None
    brake_fluid_level_warning: str | None = None
    engine_coolant_level_warning: str | None = None

    __table_args__ = (
        Index("ix_vehiclesnapshot_vin_fetched", "vin", "fetched_at"),
    )


class RawSnapshot(SQLModel, table=True):
    """Full JSON dump of every fetch — safety net for fields not yet mapped."""

    id: int | None = Field(default=None, primary_key=True)
    fetched_at: datetime = Field(index=True)
    vin: str = Field(index=True)
    payload: dict[str, Any] = Field(sa_column=Column(JSON))


class FetchError(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    occurred_at: datetime = Field(index=True)
    vin: str | None = None
    message: str


class ChargeSession(SQLModel, table=True):
    """A contiguous charging episode, detected from snapshot transitions."""

    id: int | None = Field(default=None, primary_key=True)
    vin: str = Field(index=True)
    started_at: datetime = Field(index=True)
    ended_at: datetime | None = Field(default=None, index=True)

    soc_start: int | None = None
    soc_end: int | None = None
    range_start_km: int | None = None
    range_end_km: int | None = None

    peak_power_w: int | None = None
    avg_power_w: int | None = None
    energy_kwh: float | None = None  # rough estimate from SoC delta * battery capacity

    price_ct_per_kwh: float | None = None  # user override; None ⇒ use app default
    note: str | None = None  # free text (location, operator, …)
    co2_g_per_kwh: int | None = None  # AT grid mix at session start

    is_complete: bool = Field(default=False, index=True)  # set True when charging_status transitions to Done/Idle

    # Composite for the hot query: list/aggregate sessions per VIN over a
    # time window. SQLite picks single-column indexes too slowly here.
    __table_args__ = (
        Index("ix_chargesession_vin_started", "vin", "started_at"),
    )


class AppSetting(SQLModel, table=True):
    """Generic key/value bag for user-editable app settings.

    Use the helpers in app_settings.py — don't touch this table directly.
    """

    key: str = Field(primary_key=True)
    value: str
    updated_at: datetime = Field(default_factory=utcnow)


class JourneyImport(SQLModel, table=True):
    """Trip imported from the official Polestar app's journey-log CSV export.

    The CSV gives us ground-truth fields we cannot derive from polling:
    real kWh consumed, geocoded addresses, and crisp trip boundaries the
    car itself decided. Used to verify our detected trips and to surface
    addresses on /tripbook.
    """

    id: int | None = Field(default=None, primary_key=True)
    vin: str | None = Field(default=None, index=True)
    started_at: datetime = Field(index=True)
    ended_at: datetime
    start_address: str | None = None
    end_address: str | None = None
    start_lat: float | None = None
    start_lng: float | None = None
    end_lat: float | None = None
    end_lng: float | None = None
    distance_km: float | None = None
    consumption_kwh: float | None = None
    start_odometer_km: float | None = None
    end_odometer_km: float | None = None
    soc_start: int | None = None
    soc_end: int | None = None
    trip_type: str | None = None
    category: str | None = None
    comments: str | None = None
    imported_at: datetime = Field(default_factory=utcnow)
    # Enrichments (filled lazily by post-import jobs)
    outside_temp_c: float | None = None
    weather_code: int | None = None  # Open-Meteo WMO weather code
    co2_g_per_kwh: int | None = None  # grid CO2 intensity at trip start


class ChargingStation(SQLModel, table=True):
    """A known charging station, matched by GPS proximity (500 m radius).

    Created/updated when user saves a session with a note (station name).
    Used to auto-fill note + price for future sessions at the same location.
    """

    id: int | None = Field(default=None, primary_key=True)
    name: str
    lat: float
    lng: float
    price_ct_per_kwh: float | None = None
    updated_at: datetime = Field(default_factory=utcnow)


class NotificationLog(SQLModel, table=True):
    """Dedupes notifications so we don't spam ntfy on every poll."""

    id: int | None = Field(default=None, primary_key=True)
    sent_at: datetime = Field(index=True)
    vin: str | None = None
    kind: str = Field(index=True)  # e.g. charge_complete, low_soc, service_due
    key: str = Field(index=True)  # idempotency key (e.g. session id, date)
