from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text
from sqlmodel import SQLModel, create_engine

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
            ("chargesession", "inferred", "BOOLEAN"),
        ]
    if "journeyimport" in tables:
        plan += [
            ("journeyimport", "outside_temp_c", "FLOAT"),
            ("journeyimport", "weather_code", "INTEGER"),
            ("journeyimport", "co2_g_per_kwh", "INTEGER"),
        ]
    if "chargingstation" in tables:
        plan += [
            ("chargingstation", "price_ct_per_kwh", "FLOAT"),
        ]

    with engine.begin() as conn:
        for table, name, sql_type in plan:
            existing = {c["name"] for c in insp.get_columns(table)}
            if name in existing:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))

    # Indexes — create_all skips index creation on tables that already exist.
    # CREATE INDEX IF NOT EXISTS is the cleanest forward-only equivalent.
    index_plan: list[tuple[str, str, str]] = [
        # name, table, columns
        ("ix_chargesession_vin_started", "chargesession", "vin, started_at"),
        ("ix_chargesession_is_complete", "chargesession", "is_complete"),
        ("ix_vehiclesnapshot_vin_fetched", "vehiclesnapshot", "vin, fetched_at"),
    ]
    with engine.begin() as conn:
        for name, table, cols in index_plan:
            if table in tables:
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})"))


def _relax_chargingstation_gps(engine) -> None:
    """Drop the NOT NULL constraint on chargingstation.lat/lng (forward-only).

    Stations used to require GPS; they may now be name-only (price-book
    entries added manually). SQLite cannot ALTER a column's nullability, so
    rebuild the tiny table once, guarded by the current schema and wrapped in
    a single transaction (SQLite DDL is transactional → all-or-nothing).
    """
    insp = inspect(engine)
    if "chargingstation" not in insp.get_table_names():
        return
    cols = {c["name"]: c for c in insp.get_columns("chargingstation")}
    if "lat" not in cols or cols["lat"]["nullable"]:
        return  # already nullable (new DB) or table missing → nothing to do
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE chargingstation_new ("
                "id INTEGER NOT NULL PRIMARY KEY, name VARCHAR NOT NULL, "
                "lat FLOAT, lng FLOAT, price_ct_per_kwh FLOAT, "
                "updated_at DATETIME NOT NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO chargingstation_new "
                "(id, name, lat, lng, price_ct_per_kwh, updated_at) "
                "SELECT id, name, lat, lng, price_ct_per_kwh, updated_at "
                "FROM chargingstation"
            )
        )
        conn.execute(text("DROP TABLE chargingstation"))
        conn.execute(text("ALTER TABLE chargingstation_new RENAME TO chargingstation"))


def _dedupe_journeyimport(engine) -> None:
    """Collapse duplicate imported trips on (started_at, ended_at) — forward-only.

    A journey is uniquely identified by its exact start+end timestamps. Early
    imports that ran *with* a VIN and were later re-imported *without* one
    slipped past the VIN-sensitive dedup in ``journey_import.upsert`` and
    produced exact duplicate rows. Keep the lowest id per timestamp pair and
    drop the rest. Idempotent: a no-op once the table is unique.
    """
    insp = inspect(engine)
    if "journeyimport" not in insp.get_table_names():
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM journeyimport WHERE id NOT IN "
                "(SELECT MIN(id) FROM journeyimport GROUP BY started_at, ended_at)"
            )
        )


def init_engine(db_path: Path):
    global _engine
    db_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{db_path}"
    _engine = create_engine(url, echo=False, connect_args={"check_same_thread": False})
    # Import models so SQLModel registers tables before create_all.
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(_engine)
    _ensure_columns(_engine)
    _relax_chargingstation_gps(_engine)
    _dedupe_journeyimport(_engine)
    return _engine


def get_engine():
    if _engine is None:
        raise RuntimeError("Engine not initialised; call init_engine() first")
    return _engine


