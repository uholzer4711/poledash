from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

_engine = None


def _ensure_columns(engine) -> None:
    """Tiny forward-only migration: ADD COLUMN for new fields on existing DBs.

    SQLModel.metadata.create_all() never alters existing tables. Whenever the
    Snapshot model gains a field, list it here so old SQLite files pick it up
    without losing history. SQLite allows ADD COLUMN without a default for
    nullable columns, which is what every new column we add should be.
    """
    insp = inspect(engine)
    tables = insp.get_table_names()

    plan: list[tuple[str, str, str]] = []  # (table, column, sql_type)
    if "vehiclesnapshot" in tables:
        plan += [
            ("vehiclesnapshot", "minutes_to_minimum_soc", "INTEGER"),
            ("vehiclesnapshot", "charging_voltage_v", "INTEGER"),
            ("vehiclesnapshot", "charging_type", "VARCHAR"),
            ("vehiclesnapshot", "target_soc_percent", "INTEGER"),
            ("vehiclesnapshot", "target_soc_type", "VARCHAR"),
            ("vehiclesnapshot", "internal_vehicle_id", "VARCHAR"),
            ("vehiclesnapshot", "registration_date", "VARCHAR"),
            ("vehiclesnapshot", "factory_complete_date", "VARCHAR"),
            ("vehiclesnapshot", "software_version_ts", "DATETIME"),
            ("vehiclesnapshot", "battery_voltage_v", "FLOAT"),
            ("vehiclesnapshot", "battery_capacity_kwh", "FLOAT"),
            ("vehiclesnapshot", "battery_modules", "INTEGER"),
            ("vehiclesnapshot", "battery_cells", "INTEGER"),
            ("vehiclesnapshot", "pending_target_soc_percent", "INTEGER"),
            ("vehiclesnapshot", "pending_target_soc_type", "VARCHAR"),
            ("vehiclesnapshot", "range_miles", "INTEGER"),
            ("vehiclesnapshot", "avg_consumption_kwh_per_100km_automatic", "FLOAT"),
            ("vehiclesnapshot", "avg_consumption_kwh_per_100km_since_charge", "FLOAT"),
            ("vehiclesnapshot", "total_energy_consumption_wh", "FLOAT"),
            ("vehiclesnapshot", "total_energy_consumption_wh_automatic", "FLOAT"),
            ("vehiclesnapshot", "total_energy_consumption_wh_since_charge", "FLOAT"),
            ("vehiclesnapshot", "charger_power_status_raw", "INTEGER"),
        ]
    if "chargesession" in tables:
        plan += [
            ("chargesession", "price_ct_per_kwh", "FLOAT"),
            ("chargesession", "note", "VARCHAR"),
            ("chargesession", "co2_g_per_kwh", "INTEGER"),
        ]
    if "journeyimport" in tables:
        plan += [
            ("journeyimport", "outside_temp_c", "FLOAT"),
            ("journeyimport", "weather_code", "INTEGER"),
            ("journeyimport", "co2_g_per_kwh", "INTEGER"),
        ]

    with engine.begin() as conn:
        for table, name, sql_type in plan:
            existing = {c["name"] for c in insp.get_columns(table)}
            if name in existing:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))


def init_engine(db_path: Path):
    global _engine
    db_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{db_path}"
    _engine = create_engine(url, echo=False, connect_args={"check_same_thread": False})
    # Import models so SQLModel registers tables before create_all.
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(_engine)
    _ensure_columns(_engine)
    return _engine


def get_engine():
    if _engine is None:
        raise RuntimeError("Engine not initialised; call init_engine() first")
    return _engine


def session_scope() -> Iterator[Session]:
    with Session(get_engine()) as s:
        yield s
