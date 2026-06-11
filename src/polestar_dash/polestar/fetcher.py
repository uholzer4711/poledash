from __future__ import annotations

import dataclasses
import logging
from datetime import date, datetime
from enum import Enum
from typing import Any

from ..util import charge_electrical, utcnow
from .client import PolestarClient

log = logging.getLogger(__name__)


def _to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if dataclasses.is_dataclass(obj):
        return {k: _to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return str(obj)


@dataclasses.dataclass
class Snapshot:
    """Flattened view of one fetch. None means 'not reported by this car'."""

    vin: str
    fetched_at: datetime

    # Vehicle info
    model_name: str | None = None
    model_year: str | None = None
    registration_no: str | None = None
    software_version: str | None = None
    battery_spec: str | None = None
    torque: str | None = None
    internal_vehicle_id: str | None = None
    registration_date: str | None = None
    factory_complete_date: str | None = None
    software_version_ts: datetime | None = None

    # Battery spec (structured)
    battery_voltage_v: float | None = None
    battery_capacity_kwh: float | None = None
    battery_modules: int | None = None
    battery_cells: int | None = None

    # Battery / charging
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
    charging_type: str | None = None
    target_soc_percent: int | None = None
    target_soc_type: str | None = None
    pending_target_soc_percent: int | None = None
    pending_target_soc_type: str | None = None
    battery_event_ts: datetime | None = None

    # Odometer
    odometer_km: float | None = None
    avg_speed_kmh: int | None = None
    trip_auto_km: float | None = None
    odometer_event_ts: datetime | None = None

    # Health
    service_warning: str | None = None
    days_to_service: int | None = None
    distance_to_service_km: int | None = None
    oil_level_warning: str | None = None
    brake_fluid_level_warning: str | None = None
    engine_coolant_level_warning: str | None = None

    raw: dict[str, Any] = dataclasses.field(default_factory=dict)


async def fetch_snapshot(client: PolestarClient, vin: str) -> Snapshot:
    """Refresh and collect everything the API will give us for one VIN."""
    await client.refresh(vin)

    info = client.api.get_car_information(vin=vin)
    tele = client.api.get_car_telematics(vin=vin)
    grpc_battery = None
    grpc_target = None
    car_images = None
    try:
        grpc_battery = client.api.get_grpc_battery(vin=vin)
    except Exception:  # noqa: BLE001
        log.debug("gRPC battery unavailable for %s", vin, exc_info=True)
    try:
        grpc_target = client.api.get_grpc_target_soc(vin=vin)
    except Exception:  # noqa: BLE001
        log.debug("gRPC target SoC unavailable for %s", vin, exc_info=True)
    try:
        car_images = client.api.get_car_images(vin=vin)
    except Exception:  # noqa: BLE001
        log.debug("car images unavailable for %s", vin, exc_info=True)

    snap = Snapshot(vin=vin, fetched_at=utcnow())

    if info is not None:
        snap.model_name = info.model_name
        snap.model_year = info.model_year
        snap.registration_no = info.registration_no
        snap.software_version = info.software_version
        snap.torque = info.torque
        snap.internal_vehicle_id = info.internal_vehicle_identifier
        snap.registration_date = (
            info.registration_date.isoformat() if info.registration_date else None
        )
        snap.factory_complete_date = (
            info.factory_complete_date.isoformat() if info.factory_complete_date else None
        )
        snap.software_version_ts = info.software_version_timestamp
        # info.battery is a CarBatteryInformationData with voltage/capacity/
        # modules/cells when populated. Keep `battery_spec` as the legacy
        # string for back-compat (some templates still render it).
        b = info.battery
        if b is not None:
            snap.battery_voltage_v = b.voltage
            snap.battery_capacity_kwh = b.capacity
            snap.battery_modules = b.modules
            snap.battery_cells = b.cells
            if b.capacity:
                snap.battery_spec = f"{b.capacity:g} kWh"

    if tele is not None:
        if tele.battery is not None:
            b = tele.battery
            snap.soc_percent = b.battery_charge_level_percentage
            snap.range_km = b.estimated_distance_to_empty_km
            snap.avg_consumption_kwh_per_100km = b.average_energy_consumption_kwh_per_100km
            snap.charging_status = b.charging_status.value if b.charging_status else None
            snap.charger_connection_status = (
                b.charger_connection_status.value if b.charger_connection_status else None
            )
            snap.charging_power_w = b.charging_power_watts
            snap.charging_current_a = b.charging_current_amps
            snap.minutes_to_full = b.estimated_charging_time_to_full_minutes
            snap.minutes_to_target = b.estimated_charging_time_minutes_to_target_distance
            snap.battery_event_ts = b.event_updated_timestamp

        if tele.odometer is not None:
            o = tele.odometer
            snap.odometer_km = (o.odometer_meters / 1000) if o.odometer_meters is not None else None
            snap.avg_speed_kmh = o.average_speed_km_per_hour
            snap.trip_auto_km = o.trip_meter_automatic_km
            snap.odometer_event_ts = o.event_updated_timestamp

        if tele.health is not None:
            h = tele.health
            snap.service_warning = h.service_warning.value if h.service_warning else None
            snap.days_to_service = h.days_to_service
            snap.distance_to_service_km = h.distance_to_service_km
            snap.oil_level_warning = h.oil_level_warning.value if h.oil_level_warning else None
            snap.brake_fluid_level_warning = (
                h.brake_fluid_level_warning.value if h.brake_fluid_level_warning else None
            )
            snap.engine_coolant_level_warning = (
                h.engine_coolant_level_warning.value if h.engine_coolant_level_warning else None
            )

    if grpc_battery is not None:
        # Polestar dropped chargingStatus from BatteryV2 (2026-05-21); gRPC
        # still reports it, so prefer that source.
        if getattr(grpc_battery, "charging_status", None) is not None:
            cs = grpc_battery.charging_status
            cs_val = cs.value if hasattr(cs, "value") else str(cs)
            if cs_val and cs_val != "Unspecified":
                snap.charging_status = cs_val
        if grpc_battery.charging_voltage_volts:
            snap.charging_voltage_v = grpc_battery.charging_voltage_volts
        # The widened consumption/range fields our pypolestar patch injects
        # (total_energy_consumption_wh & friends) stay un-extracted: the API
        # never populates them for this car (all-NULL history). They remain
        # inspectable via /api/lab/grpc-battery-raw and the raw payload.
        cps = getattr(grpc_battery, "charger_power_status_raw", None)
        snap.charger_power_status_raw = cps if cps is not None else None
        if grpc_battery.charging_type:
            ct = grpc_battery.charging_type.value
            snap.charging_type = ct if ct != "Unspecified" else None
        if grpc_battery.estimated_charging_time_minutes_to_minimum_soc:
            snap.minutes_to_minimum_soc = (
                grpc_battery.estimated_charging_time_minutes_to_minimum_soc
            )
        # Fall back to gRPC values when GraphQL didn't surface them
        if snap.charging_power_w is None and grpc_battery.charging_power_watts:
            snap.charging_power_w = grpc_battery.charging_power_watts
        if snap.charging_current_a is None and grpc_battery.charging_current_amps:
            snap.charging_current_a = grpc_battery.charging_current_amps
        if (
            snap.avg_consumption_kwh_per_100km is None
            and grpc_battery.average_energy_consumption_kwh_per_100km
        ):
            snap.avg_consumption_kwh_per_100km = (
                grpc_battery.average_energy_consumption_kwh_per_100km
            )

    # Last-ditch derivation if neither GraphQL nor gRPC gave us a status.
    if snap.charging_status is None:
        if snap.charging_power_w and snap.charging_power_w > 100:
            snap.charging_status = "Charging"
        elif snap.charger_connection_status == "Connected":
            snap.charging_status = "Idle"

    # The gRPC charging_type enum mislabels ordinary 11 kW 3-phase AC as "DC".
    # Correct the typed field from physics (low per-phase voltage at modest
    # power ⇒ AC; high bus voltage / high power ⇒ DC) so History charts and
    # labels don't read "DC" for an AC charge. The RawSnapshot keeps the
    # original enum value untouched.
    elec = charge_electrical(
        snap.charging_power_w, snap.charging_voltage_v, snap.charging_current_a,
        api_type=snap.charging_type,
    )
    if elec["mode"]:
        snap.charging_type = elec["mode"]

    if grpc_target is not None:
        snap.target_soc_percent = grpc_target.battery_charge_target_level
        if grpc_target.charge_target_level_setting_type:
            tt = grpc_target.charge_target_level_setting_type.value
            snap.target_soc_type = tt if tt != "Unspecified" else None
        snap.pending_target_soc_percent = grpc_target.pending_battery_charge_target_level
        if grpc_target.pending_charge_target_level_setting_type:
            pt = grpc_target.pending_charge_target_level_setting_type.value
            snap.pending_target_soc_type = pt if pt != "Unspecified" else None

    # car_images is only kept for the raw payload (safety net) — the API never
    # returned a usable image for this car, and the dashboard image comes from
    # the upload / CAR_IMAGE_URL / built-in SVG chain (templating.car_image_src).
    snap.raw = {
        "information": _to_jsonable(info),
        "telematics": _to_jsonable(tele),
        "grpc_battery": _to_jsonable(grpc_battery),
        "grpc_target_soc": _to_jsonable(grpc_target),
        "images": _to_jsonable(car_images),
    }
    return snap
