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
    avg_consumption_kwh_per_100km: float | None = None
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


class SohSnapshot(SQLModel, table=True):
    """Daily snapshot of the estimated battery State-of-Health, so Battery &
    Charging can chart the trend. Computed by the scheduler (``compute_soh``);
    at most one row per day (the daily job updates today's row in place)."""

    id: int | None = Field(default=None, primary_key=True)
    computed_at: datetime = Field(index=True)
    soh_pct: int
    implied_kwh: float
    nominal_kwh: float
    samples: int


class LogEntry(SQLModel, table=True):
    """Persisted copy of the in-memory activity log (``logbuffer`` ring) so the
    /log tab can show history beyond the last 2000 lines and across restarts.
    Flushed in batches by the scheduler (``_persist_logs``) and pruned by
    retention (``log_retention_days``)."""

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(index=True)
    level: str
    # No standalone index on levelno: the history query orders by ts and the
    # level filter (≥ INFO) matches almost every row, so ix_logentry_ts carries
    # it. A levelno index would only add write cost.
    levelno: int
    logger: str = Field(index=True)
    msg: str
    exc: str | None = None


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
    # Stamped at close from the nearest C3Snapshot in the session window
    # (the car's own GPS + on-car environment). lat/lng also short-circuits
    # the JourneyImport location heuristic in stations.locate_session.
    lat: float | None = None
    lng: float | None = None
    outside_temp_c: float | None = None  # on-car temperature while charging
    air_pm25: float | None = None  # cabin PM2.5 while charging
    # Odometer at close (nearest VehicleSnapshot), used for the Spritmonitor
    # export's "trip" field (km driven since the previous session).
    odometer_km: float | None = None
    # When this session was last successfully pushed to Spritmonitor.de as a
    # fueling — None means never exported. See spritmonitor.py.
    spritmonitor_exported_at: datetime | None = None

    is_complete: bool = Field(default=False, index=True)  # set True when charging_status transitions to Done/Idle
    # True for sessions opened heuristically from a SoC rise (the API never
    # reported "Charging"). These don't get a terminal "Done"/"Idle" signal, so
    # they must not be closed on a bare Idle plateau — only on a SoC drop, the
    # car driving off, or the 24 h timeout.
    inferred: bool = Field(default=False)

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
    # True if the importer auto-converted this row's consumption from a
    # Wh-mislabelled export to kWh; NULL on rows imported before the flag
    # existed. Keeps the unit conversion auditable after the fact.
    consumption_scaled: bool | None = None
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
    air_pm25: float | None = None  # cabin PM2.5 from nearest C3 snapshot


class ChargingStation(SQLModel, table=True):
    """A known charging station, matched by GPS proximity (500 m radius).

    Created/updated when user saves a session with a note (station name).
    Used to auto-fill note + price for future sessions at the same location.
    """

    id: int | None = Field(default=None, primary_key=True)
    name: str
    # GPS is optional: a station may be name-only (manually added as a known
    # place / price-book entry). Coordinates, when present, drive the 500 m
    # GPS auto-tag of charge sessions; name-only entries are still selectable
    # from the session Location dropdown.
    lat: float | None = None
    lng: float | None = None
    price_ct_per_kwh: float | None = None
    updated_at: datetime = Field(default_factory=utcnow)


class SoftwareVersionChange(SQLModel, table=True):
    """One row per detected change of the car's *installed* software version.

    Lets the dashboard answer "which version, since when" and keeps a full
    history of OTA installs. Detected at poll time by comparing the latest
    snapshot's software_version against the previous recorded value.
    """

    id: int | None = Field(default=None, primary_key=True)
    vin: str = Field(index=True)
    detected_at: datetime = Field(index=True)  # when we first polled the new version
    software_version: str
    previous_version: str | None = None
    software_version_ts: datetime | None = None  # car-reported timestamp for the version

    __table_args__ = (
        Index("ix_swversionchange_vin_detected", "vin", "detected_at"),
    )


class SoftwareRelease(SQLModel, table=True):
    """A software version published in the Polestar online manual.

    Scraped twice a day from SOFTWARE_MANUAL_URL. `version` is the raw label
    (e.g. "P5.1.9"); `version_key` is the zero-padded sortable form so plain
    SQL/string ordering matches semantic version order.
    """

    version: str = Field(primary_key=True)
    version_key: str = Field(index=True)
    notes: str | None = None
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)


class NotificationLog(SQLModel, table=True):
    """Dedupes notifications so we don't spam ntfy on every poll."""

    id: int | None = Field(default=None, primary_key=True)
    sent_at: datetime = Field(index=True)
    vin: str | None = None
    kind: str = Field(index=True)  # e.g. charge_complete, low_soc, service_due
    key: str = Field(index=True)  # idempotency key (e.g. session id, date)


class PendingAction(SQLModel, table=True):
    """A deferred C3 command awaiting one-tap confirmation.

    Created when an automation rule in "vorschlag" (suggest) mode fires: the
    push notification carries a link to /confirm/{token}; tapping it (behind the
    same reverse-proxy auth as the whole app) consumes the token and runs the
    command. The token is a one-time intent guard, never authentication — the
    confirm route is protected by the proxy like every other route.
    """

    token: str = Field(primary_key=True)
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime = Field(index=True)
    rule_id: str
    command: str
    params_json: str = "{}"
    vin: str | None = None
    status: str = "pending"  # pending | consumed | done | expired
    consumed_at: datetime | None = None
    result_json: str | None = None


class C3Snapshot(SQLModel, table=True):
    """Structured snapshot from the C3 (Volvo Cloud Connectivity) backend.

    One row per successful C3 poll. Surfaces fields pypolestar does not expose:
    live GPS, exterior door/window/lock state, climate, cabin air, on-car
    weather. Persisted on its own (slower, staggered) poll cadence — kept
    separate from VehicleSnapshot because it comes from a different library and
    a different cadence, and we never want a C3 outage to touch the main loop.
    Enum-typed fields store the decoded string ("open"/"closed"/"locked"/…),
    matching the live /test payload so the same render code works for both.
    """

    id: int | None = Field(default=None, primary_key=True)
    fetched_at: datetime = Field(index=True)
    vin: str = Field(index=True)

    # Location (net-new vs pypolestar)
    lat: float | None = None
    lng: float | None = None
    heading: float | None = None
    speed: float | None = None
    gps_ts: int | None = None  # car-reported Unix seconds
    parked_lat: float | None = None
    parked_lng: float | None = None
    parked_ts: int | None = None

    # On-car weather
    weather_temp_c: float | None = None
    weather_ts: int | None = None  # Unix millis

    # Exterior — lock + openings (decoded strings: open/closed/ajar, locked/…)
    is_locked: bool | None = None
    any_door_open: bool | None = None
    central_lock: str | None = None
    door_fl: str | None = None
    door_fr: str | None = None
    door_rl: str | None = None
    door_rr: str | None = None
    win_fl: str | None = None
    win_fr: str | None = None
    win_rl: str | None = None
    win_rr: str | None = None
    sunroof: str | None = None
    hood: str | None = None  # frunk
    tailgate: str | None = None  # boot / trunk
    tank_lid: str | None = None  # charge-port flap

    # Climate
    climate_active: bool | None = None
    climate_action: str | None = None
    climate_running: str | None = None
    climate_time_remaining: int | None = None
    climate_current_temp_c: float | None = None
    climate_target_temp_c: float | None = None
    seat_heat_fl: str | None = None
    seat_heat_fr: str | None = None
    seat_heat_rl: str | None = None
    seat_heat_rr: str | None = None
    steering_wheel_heat: str | None = None

    # Cabin air quality
    air_running: bool | None = None
    air_aqi: float | None = None
    air_pm25: float | None = None

    # Extended health (light/tyre net-new; rest mirrors VehicleSnapshot)
    any_light_failure: bool | None = None
    any_tyre_warning: bool | None = None
    service_warning: str | None = None
    days_to_service: int | None = None
    distance_to_service_km: int | None = None
    brake_fluid: str | None = None
    oil: str | None = None
    coolant: str | None = None
    washer_fluid: str | None = None
    low_voltage_battery: str | None = None  # 12V battery warning (ok/warning/…)
    engine_hours_to_service: int | None = None
    failed_lights: str | None = None  # comma-joined bulb names reporting a fault

    # Battery / charging (C3 view — often fresher than pypolestar)
    charge_level: int | None = None
    range_km: int | None = None
    is_plugged_in: bool | None = None
    is_charging: bool | None = None
    charging_status: str | None = None
    charging_type: str | None = None
    connection: str | None = None
    # The car's own charge-time horizons, minutes (0 = "no estimate" → stored
    # as NULL). Only populate while charging.
    time_to_full: int | None = None
    time_to_target: int | None = None
    time_to_min_soc: int | None = None
    amp_limit: float | None = None
    odometer_km: float | None = None
    availability_status: str | None = None
    usage_mode: str | None = None
    unavailable_reason: str | None = None

    # Tyre pressure kPa (0.0 from proto3 defaults → stored NULL)
    tyre_fl_kpa: float | None = None
    tyre_fr_kpa: float | None = None
    tyre_rl_kpa: float | None = None
    tyre_rr_kpa: float | None = None

    # TCU connectivity
    tcu_status: str | None = None
    tcu_network: str | None = None
    tcu_signal: str | None = None

    __table_args__ = (
        Index("ix_c3snapshot_vin_fetched", "vin", "fetched_at"),
    )


class C3SoftwareEvent(SQLModel, table=True):
    """History of the car-reported OTA update state (C3 ``get_software_info``).

    The live C3 cache is the only other place this exists — a restart used to
    wipe it. One row per observed (version, state) transition, so /software can
    show when an update appeared, downloaded, failed or installed. Complements
    SoftwareVersionChange, which only tracks the *installed* version.
    """

    id: int | None = Field(default=None, primary_key=True)
    vin: str = Field(index=True)
    seen_at: datetime = Field(index=True)
    version: str
    state_code: int
    state: str
    name: str | None = None
    notes: str | None = None


class LightWarningEvent(SQLModel, table=True):
    """Per-bulb fault history (C3 health). C3Snapshot only keeps the current
    comma-joined ``failed_lights`` string — this records each lamp's fault /
    cleared transitions so "which bulb failed first, and when" stays
    reconstructible for the workshop."""

    id: int | None = Field(default=None, primary_key=True)
    vin: str = Field(index=True)
    detected_at: datetime = Field(index=True)
    lamp: str  # human label from c3._LIGHT_FIELDS (e.g. "brake L")
    event: str  # "fault" | "cleared"


class RawC3Snapshot(SQLModel, table=True):
    """Full JSON dump of every C3 fetch — safety net, mirrors RawSnapshot."""

    id: int | None = Field(default=None, primary_key=True)
    fetched_at: datetime = Field(index=True)
    vin: str = Field(index=True)
    payload: dict[str, Any] = Field(sa_column=Column(JSON))
