from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from polestar_dash.polestar.client import PolestarClient
from polestar_dash.polestar.fetcher import fetch_snapshot


def _fake_info():
    return SimpleNamespace(
        model_name="Polestar 2",
        model_year="2024",
        registration_no="W-12345",
        software_version="P3.2",
        battery=SimpleNamespace(voltage=400.0, capacity=78.0, modules=27, cells=324),
        torque="660 Nm",
        internal_vehicle_identifier="INT-VIN-1",
        registration_date=None,
        factory_complete_date=None,
        software_version_timestamp=None,
    )


def _fake_telematics():
    battery = SimpleNamespace(
        battery_charge_level_percentage=72,
        estimated_distance_to_empty_km=310,
        average_energy_consumption_kwh_per_100km=18.4,
        charging_status=SimpleNamespace(value="Charging"),
        charger_connection_status=SimpleNamespace(value="Connected"),
        charging_power_watts=11000,
        charging_current_amps=16,
        estimated_charging_time_to_full_minutes=90,
        estimated_charging_time_minutes_to_target_distance=45,
        event_updated_timestamp=datetime(2026, 5, 19, 12, 0, 0),
    )
    odometer = SimpleNamespace(
        odometer_meters=42_000_000,
        average_speed_km_per_hour=48,
        trip_meter_automatic_km=123.4,
        trip_meter_manual_km=5.6,
        event_updated_timestamp=datetime(2026, 5, 19, 12, 0, 0),
    )
    health = SimpleNamespace(
        service_warning=SimpleNamespace(value="NoWarning"),
        days_to_service=120,
        distance_to_service_km=8000,
        oil_level_warning=SimpleNamespace(value="NoWarning"),
        brake_fluid_level_warning=SimpleNamespace(value="NoWarning"),
        engine_coolant_level_warning=SimpleNamespace(value="NoWarning"),
    )
    return SimpleNamespace(battery=battery, odometer=odometer, health=health)


@pytest.mark.asyncio
async def test_fetch_snapshot_maps_fields():
    client = PolestarClient(email="x", password="y", vins=["VIN1"])
    fake_api = MagicMock()
    fake_api.update_latest_data = AsyncMock()
    fake_api.get_car_information = MagicMock(return_value=_fake_info())
    fake_api.get_car_telematics = MagicMock(return_value=_fake_telematics())
    # gRPC sources are optional and guarded; treat them as unavailable so this
    # test pins the car-information / telematics mapping path.
    fake_api.get_grpc_battery = MagicMock(return_value=None)
    fake_api.get_grpc_target_soc = MagicMock(return_value=None)
    fake_api.get_car_images = MagicMock(return_value=None)
    client._api = fake_api
    client._vins = ["VIN1"]

    snap = await fetch_snapshot(client, "VIN1")

    assert snap.vin == "VIN1"
    assert snap.model_name == "Polestar 2"
    assert snap.soc_percent == 72
    assert snap.range_km == 310
    assert snap.charging_status == "Charging"
    assert snap.charger_connection_status == "Connected"
    assert snap.charging_power_w == 11000
    assert snap.odometer_km == 42000.0
    assert snap.trip_auto_km == 123.4
    assert snap.service_warning == "NoWarning"
    assert "information" in snap.raw and "telematics" in snap.raw
    fake_api.update_latest_data.assert_awaited_once_with(
        vin="VIN1", update_vehicle=True, update_telematics=True, update_grpc=True
    )
