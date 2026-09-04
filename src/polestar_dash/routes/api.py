from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlmodel import Session, select

from ..app_settings import (
    ALERT_DEFAULTS,
    all_alert_configs,
    blended_price_ct,
    get_alert_config,
    get_charging_loss_factor,
    get_default_price_ct,
    set_alert_config,
    set_charging_loss_factor,
    set_default_price_ct,
)
from ..c3_anomalies import disconnect_gap
from ..config import get_settings
from ..db.models import (
    C3Snapshot,
    ChargeSession,
    ChargingStation,
    JourneyImport,
    LogEntry,
    SohSnapshot,
    VehicleSnapshot,
)
from ..db.session import get_engine
from ..i18n import SUPPORTED_LANGS, get_language, set_language
from ..i18n import t as t_  # plain `t` collides with this file's trip loop variable
from ..idle_drain import diagnose_idle_drains, standby_baseline
from ..insights_extras import (
    compute_air_exposure,
    compute_battery_trend,
    compute_c3_env_timeline,
    compute_charge_heatmap,
    compute_charge_temp_scatter,
    compute_co2,
    compute_forecast,
    compute_monthly_charging_cost,
    compute_range_factor,
    compute_soh,
    compute_temp_scatter,
    compute_trip_cost,
)
from ..insights_narrative import build_narrative
from ..journey_import import (
    calibrate_battery_kwh,
    geo_label_for,
    get_address_geofences,
    get_address_labels,
    label_for,
    list_imports,
    parse_csv,
    set_address_geofences,
    set_address_labels,
    tag_co2_for_imports,
    upsert,
)
from ..notify import notify
from ..polestar.c3 import c3_dict_to_row, get_c3_snapshot
from ..polestar.c3_control import run_command
from ..polestar.control_verify import is_verifiable, verify_command, verify_command_with_retries
from ..routes_stats import list_routes, trips_for_route
from ..tripbook import detect_trips
from ..util import fmt_local, utcnow
from ..weather import enrich_pending as enrich_weather_pending


def _session_to_dict(cs: ChargeSession, default_price: float) -> dict:
    d = cs.model_dump(mode="json")
    price = cs.price_ct_per_kwh if cs.price_ct_per_kwh is not None else default_price
    energy = cs.energy_kwh or 0
    d["effective_price_ct_per_kwh"] = price
    d["price_source"] = "session" if cs.price_ct_per_kwh is not None else "default"
    d["cost_eur"] = round(energy * price / 100.0, 2) if energy else 0.0
    return d

router = APIRouter(prefix="/api", tags=["api"])
log = logging.getLogger(__name__)

ALLOWED_FIELDS = {
    "soc_percent",
    "range_km",
    "charging_power_w",
    "charging_current_a",
    "charging_voltage_v",
    "avg_consumption_kwh_per_100km",
    "odometer_km",
    "avg_speed_kmh",
    "minutes_to_full",
    "minutes_to_target",
    "minutes_to_minimum_soc",
    "target_soc_percent",
}


@router.get("/latest")
def latest(vin: str | None = None) -> dict:
    with Session(get_engine()) as s:
        stmt = select(VehicleSnapshot).order_by(VehicleSnapshot.fetched_at.desc())
        if vin:
            stmt = stmt.where(VehicleSnapshot.vin == vin)
        row = s.exec(stmt.limit(1)).first()
    if row is None:
        raise HTTPException(404, "No snapshots yet")
    return row.model_dump(mode="json")


@router.get("/vins")
def vins() -> list[str]:
    with Session(get_engine()) as s:
        rows = s.exec(select(VehicleSnapshot.vin).distinct()).all()
    return list(rows)


@router.get("/software")
def software(vin: str | None = None) -> dict:
    from ..software import dashboard_state

    st = dashboard_state(vin)

    def _release(r) -> dict:
        return {
            "version": r.version,
            "notes": r.notes,
            "first_seen_at": r.first_seen_at.isoformat() if r.first_seen_at else None,
            "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
        }

    def _change(c) -> dict:
        return {
            "software_version": c.software_version,
            "previous_version": c.previous_version,
            "detected_at": c.detected_at.isoformat() if c.detected_at else None,
            "software_version_ts": (
                c.software_version_ts.isoformat() if c.software_version_ts else None
            ),
        }

    return {
        "installed": st["installed"],
        "installed_since": (
            st["installed_since"].isoformat() if st["installed_since"] else None
        ),
        "vin": st["vin"],
        "update_available": st["update_available"],
        "latest": _release(st["latest"]) if st["latest"] else None,
        "checked_at": st["checked_at"].isoformat() if st["checked_at"] else None,
        "history": [_change(c) for c in st["history"]],
        "releases": [_release(r) for r in st["releases"]],
    }


@router.post("/software/installed")
def set_software_installed(version: str = Form(...)) -> dict:
    from ..software import dashboard_state, set_manual_installed_version

    try:
        set_manual_installed_version(version)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    st = dashboard_state()
    return {
        "installed": st["installed"],
        "update_available": st["update_available"],
        "latest": st["latest"].version if st["latest"] else None,
    }


@router.get("/history")
def history(
    field: str = Query(..., description="Field name"),
    days: int = Query(7, ge=1, le=365),
    vin: str | None = None,
) -> dict:
    if field not in ALLOWED_FIELDS:
        raise HTTPException(400, f"Field must be one of {sorted(ALLOWED_FIELDS)}")
    since = utcnow() - timedelta(days=days)
    column = getattr(VehicleSnapshot, field)
    with Session(get_engine()) as s:
        stmt = (
            select(VehicleSnapshot.fetched_at, column)
            .where(VehicleSnapshot.fetched_at >= since)
            .where(column.is_not(None))
            .order_by(VehicleSnapshot.fetched_at.asc())
        )
        if vin:
            stmt = stmt.where(VehicleSnapshot.vin == vin)
        rows = s.exec(stmt).all()
    return {
        "field": field,
        "points": [{"t": t.isoformat(), "v": v} for t, v in rows],
    }


@router.get("/sessions")
def sessions(days: int = Query(60, ge=1, le=365), vin: str | None = None) -> dict:
    from ..stations import batch_suggest

    since = utcnow() - timedelta(days=days)
    default_price = get_default_price_ct()
    with Session(get_engine()) as s:
        stmt = (
            select(ChargeSession)
            .where(ChargeSession.started_at >= since)
            .order_by(ChargeSession.started_at.desc())
        )
        if vin:
            stmt = stmt.where(ChargeSession.vin == vin)
        rows = s.exec(stmt).all()
    suggestions = batch_suggest(rows)
    items = []
    for r in rows:
        d = _session_to_dict(r, default_price)
        if r.id in suggestions:
            d["station_suggestion"] = suggestions[r.id]
        items.append(d)
    total_kwh = round(sum((r.energy_kwh or 0) for r in rows), 1)
    total_cost = round(sum(i["cost_eur"] for i in items), 2)
    return {
        "default_price_ct_per_kwh": default_price,
        "items": items,
        "totals": {"kwh": total_kwh, "cost_eur": total_cost, "count": len(items)},
    }


@router.get("/sessions/{session_id}")
def session_detail(session_id: int) -> dict:
    """One session with its full charging curve (snapshots between start+end)."""
    default_price = get_default_price_ct()
    with Session(get_engine()) as s:
        cs = s.get(ChargeSession, session_id)
        if cs is None:
            raise HTTPException(404, "session not found")
        # Window: from started_at to ended_at (or +6h if still live).
        end = cs.ended_at or (cs.started_at + timedelta(hours=6))
        stmt = (
            select(VehicleSnapshot)
            .where(VehicleSnapshot.vin == cs.vin)
            .where(VehicleSnapshot.fetched_at >= cs.started_at)
            .where(VehicleSnapshot.fetched_at <= end)
            .order_by(VehicleSnapshot.fetched_at.asc())
        )
        snaps = s.exec(stmt).all()

    curve = [
        {
            "t": snap.fetched_at.isoformat(),
            "soc": snap.soc_percent,
            "power_w": snap.charging_power_w,
            "current_a": snap.charging_current_a,
            "voltage_v": snap.charging_voltage_v,
            "range_km": snap.range_km,
            "status": snap.charging_status,
        }
        for snap in snaps
    ]
    # Derived stats from curve
    powers = [c["power_w"] for c in curve if c["power_w"]]
    duration_min = None
    if cs.started_at and (cs.ended_at or curve):
        end_ts = cs.ended_at or (snaps[-1].fetched_at if snaps else cs.started_at)
        duration_min = max(0, int((end_ts - cs.started_at).total_seconds() // 60))
    return {
        "session": _session_to_dict(cs, default_price),
        "curve": curve,
        "stats": {
            "samples": len(curve),
            "duration_min": duration_min,
            "peak_power_w": max(powers) if powers else None,
            "avg_power_w": int(sum(powers) / len(powers)) if powers else None,
            "charging_type": next(
                (snap.charging_type for snap in snaps if snap.charging_type), None
            ),
        },
    }


@router.patch("/sessions/{session_id}")
def patch_session(
    session_id: int,
    price_ct_per_kwh: float | None = None,
    note: str | None = None,
    save_place: bool = False,
) -> dict:
    """Update price / note for one charging session. price=null clears the override.

    When a note is saved and the session's GPS location can be inferred from
    imported trips, a ChargingStation is created/updated so future sessions at
    the same spot (~500 m) auto-fill name + price. When ``save_place`` is set
    (user explicitly added a new place) but no GPS is available, the place is
    still remembered name-only so it appears in the Location dropdown.
    """
    from ..stations import locate_session, upsert_named_station, upsert_station

    if price_ct_per_kwh is not None and not (0 <= price_ct_per_kwh <= 500):
        raise HTTPException(400, "price_ct_per_kwh out of range")
    with Session(get_engine()) as s:
        row = s.get(ChargeSession, session_id)
        if row is None:
            raise HTTPException(404, "session not found")
        row.price_ct_per_kwh = price_ct_per_kwh
        if note is not None:
            row.note = note or None
        s.add(row)
        s.commit()
        s.refresh(row)

    effective_note = row.note
    if effective_note:
        coords = locate_session(row)
        if coords:
            upsert_station(effective_note, coords[0], coords[1], price_ct_per_kwh)
        elif save_place:
            upsert_named_station(effective_note, price_ct_per_kwh)

    return _session_to_dict(row, get_default_price_ct())


@router.get("/charging-stations")
def charging_stations_list() -> list[dict]:
    from ..stations import list_stations
    return [st.model_dump(mode="json") for st in list_stations()]


@router.post("/charging-stations")
def charging_station_create(
    name: str = Query(..., min_length=1),
    price_ct_per_kwh: float | None = None,
) -> dict:
    """Add a known place by name (no GPS). Updates price if the name exists."""
    from ..stations import upsert_named_station

    if price_ct_per_kwh is not None and not (0 <= price_ct_per_kwh <= 500):
        raise HTTPException(400, "price_ct_per_kwh out of range")
    if not name.strip():
        raise HTTPException(400, "name required")
    st = upsert_named_station(name, price_ct_per_kwh)
    return st.model_dump(mode="json")


@router.patch("/charging-stations/{station_id}")
def charging_station_patch(
    station_id: int,
    name: str | None = None,
    price_ct_per_kwh: float | None = None,
) -> dict:
    if price_ct_per_kwh is not None and not (0 <= price_ct_per_kwh <= 500):
        raise HTTPException(400, "price_ct_per_kwh out of range")
    with Session(get_engine()) as s:
        st = s.get(ChargingStation, station_id)
        if st is None:
            raise HTTPException(404, "station not found")
        if name is not None:
            st.name = name
        if price_ct_per_kwh is not None:
            st.price_ct_per_kwh = price_ct_per_kwh
        from ..util import utcnow as _utcnow
        st.updated_at = _utcnow()
        s.add(st)
        s.commit()
        s.refresh(st)
    return st.model_dump(mode="json")


@router.delete("/charging-stations/{station_id}")
def charging_station_delete(station_id: int) -> dict:
    with Session(get_engine()) as s:
        st = s.get(ChargingStation, station_id)
        if st is None:
            raise HTTPException(404, "station not found")
        s.delete(st)
        s.commit()
    return {"deleted": station_id}


@router.get("/app-settings")
def app_settings_get() -> dict:
    return {
        "default_price_ct_per_kwh": get_default_price_ct(),
        "charging_loss_factor": get_charging_loss_factor(),
    }


@router.post("/app-settings/price")
def app_settings_set_price(value: float = Query(..., ge=0, le=500)) -> dict:
    set_default_price_ct(value)
    return {"default_price_ct_per_kwh": get_default_price_ct()}


@router.post("/app-settings/charging-loss-factor")
def app_settings_set_loss_factor(value: float = Query(..., ge=1.0, le=1.5)) -> dict:
    set_charging_loss_factor(value)
    _recalc_session_energy()
    return {"charging_loss_factor": get_charging_loss_factor()}


@router.post("/app-settings/petrol")
def app_settings_set_petrol(
    price_eur_per_l: float | None = Query(None, ge=0, le=10),
    l_per_100km: float | None = Query(None, ge=0, le=30),
    co2_g_per_km: float | None = Query(None, ge=0, le=500),
) -> dict:
    """Petrol-car reference for the Insights ICE comparison (cost + CO2)."""
    from ..app_settings import get_petrol_params, set_petrol_params

    set_petrol_params(price_eur_per_l, l_per_100km, co2_g_per_km)
    return get_petrol_params()


@router.get("/app-settings/language")
def app_settings_get_language() -> dict:
    return {"language": get_language(), "supported": list(SUPPORTED_LANGS)}


@router.post("/app-settings/language")
def app_settings_set_language(value: str = Query(...)) -> dict:
    if value not in SUPPORTED_LANGS:
        raise HTTPException(status_code=400, detail=f"Unsupported language: {value}")
    set_language(value)
    return {"language": get_language()}


# --- Spritmonitor.de export ----------------------------------------------------
# Manual + automatic export of ChargeSessions as Spritmonitor "fuelings". See
# spritmonitor.py for the API client + field mapping.


@router.get("/spritmonitor/status")
def spritmonitor_status() -> dict:
    from ..app_settings import (
        get_spritmonitor_auto_export,
        get_spritmonitor_credentials,
        get_spritmonitor_lookup_ids,
        get_spritmonitor_vehicle,
    )

    creds = get_spritmonitor_credentials()
    vehicle = get_spritmonitor_vehicle()
    return {
        "credentials_set": creds is not None,
        "vehicle": vehicle,
        "lookup_ids": get_spritmonitor_lookup_ids(),
        "auto_export": get_spritmonitor_auto_export(),
        "configured": creds is not None and vehicle is not None,
    }


@router.post("/spritmonitor/credentials")
def spritmonitor_set_credentials(
    bearer_token: str = Query(...), app_token: str = Query("")
) -> dict:
    from ..app_settings import set_spritmonitor_credentials

    try:
        set_spritmonitor_credentials(bearer_token, app_token)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"credentials_set": True}


@router.post("/spritmonitor/test")
def spritmonitor_test() -> dict:
    from ..app_settings import get_spritmonitor_credentials
    from ..spritmonitor import SpritmonitorError, test_connection

    creds = get_spritmonitor_credentials()
    if creds is None:
        raise HTTPException(400, "Save your bearer token first")
    try:
        vehicles = test_connection(*creds)
    except SpritmonitorError as e:
        raise HTTPException(502, str(e)) from e
    return {"vehicles": vehicles}


@router.get("/spritmonitor/tanks")
def spritmonitor_tanks(vehicle_id: str = Query(...)) -> dict:
    from ..app_settings import get_spritmonitor_credentials
    from ..spritmonitor import SpritmonitorError, list_tanks

    creds = get_spritmonitor_credentials()
    if creds is None:
        raise HTTPException(400, "Save your bearer token first")
    try:
        tanks = list_tanks(*creds, vehicle_id)
    except SpritmonitorError as e:
        raise HTTPException(502, str(e)) from e
    return {"tanks": tanks}


@router.post("/spritmonitor/vehicle")
def spritmonitor_set_vehicle(
    vehicle_id: str = Query(...),
    tank_id: str = Query(...),
    vehicle_name: str | None = Query(None),
) -> dict:
    from ..app_settings import get_spritmonitor_credentials, set_spritmonitor_vehicle
    from ..spritmonitor import SpritmonitorError, detect_lookup_ids

    set_spritmonitor_vehicle(vehicle_id, tank_id, vehicle_name)
    creds = get_spritmonitor_credentials()
    lookup = None
    if creds is not None:
        try:
            lookup = detect_lookup_ids(*creds)
        except SpritmonitorError as e:
            # Vehicle/tank are saved either way; surface the detection
            # failure so the settings UI can explain what to check.
            raise HTTPException(502, str(e)) from e
    return {"vehicle_id": vehicle_id, "tank_id": tank_id, "lookup_ids": lookup}


@router.post("/spritmonitor/auto-export")
def spritmonitor_set_auto_export(value: bool = Query(...)) -> dict:
    from ..app_settings import set_spritmonitor_auto_export

    set_spritmonitor_auto_export(value)
    return {"auto_export": value}


@router.get("/spritmonitor/sessions")
def spritmonitor_sessions_list() -> dict:
    from ..spritmonitor import exported_sessions, pending_sessions

    default_price = get_default_price_ct()
    pending = [_session_to_dict(cs, default_price) for cs in pending_sessions()]
    exported = [_session_to_dict(cs, default_price) for cs in exported_sessions()]
    return {"pending": pending, "exported": exported}


@router.post("/sessions/{session_id}/export-spritmonitor")
def spritmonitor_export_session(session_id: int) -> dict:
    from ..spritmonitor import SpritmonitorError, SpritmonitorNotConfigured, export_session

    try:
        result = export_session(session_id)
    except SpritmonitorNotConfigured as e:
        raise HTTPException(400, str(e)) from e
    except SpritmonitorError as e:
        raise HTTPException(502, str(e)) from e
    return {"status": "exported", "result": result}


@router.post("/spritmonitor/export-all")
def spritmonitor_export_all() -> dict:
    """Export every pending session, oldest first — Spritmonitor marks the
    very first fueling as the trip-chain anchor, so it must be the earliest
    session, not whichever the user happens to click first."""
    from ..spritmonitor import (
        SpritmonitorError,
        SpritmonitorNotConfigured,
        export_session,
        pending_sessions,
    )

    exported = 0
    errors: list[str] = []
    for cs in sorted(pending_sessions(), key=lambda c: c.started_at):
        try:
            export_session(cs.id)  # type: ignore[arg-type]
            exported += 1
        except SpritmonitorNotConfigured as e:
            raise HTTPException(400, str(e)) from e
        except SpritmonitorError as e:
            errors.append(f"#{cs.id}: {e}")
    return {"exported": exported, "failed": len(errors), "errors": errors}


# --- C3 remote control (shared by dashboard + Battery&Charging panels) --------
# The canonical command endpoint. `run_command` carries its own allowlist
# (c3_control._COMMANDS), so an unknown command is rejected there — this stays a
# thin pass-through. `/api/test/control` (routes/test.py) is the legacy alias.


class ControlRequest(BaseModel):
    command: str
    params: dict | None = None


# English source strings only — keys for translations_de.py.
_COMMAND_LABELS: dict[str, str] = {
    "lock": "Lock", "unlock": "Unlock", "unlock_trunk": "Unlock trunk",
    "windows_open": "Open windows", "windows_close": "Close windows",
    "climate_start": "Start climate", "climate_stop": "Stop climate",
    "precleaning_start": "Start air pre-cleaning", "precleaning_stop": "Stop pre-cleaning",
    "charge_start": "Start charging", "charge_stop": "Stop charging",
}


async def _notify_control_result(command: str, params: dict, key: str) -> None:
    """Confirm a manually-fired command server-side and push it via ntfy/Telegram.

    The dashboard's in-page status line (control.js) polls /api/control/verify
    itself, but that poll dies the moment iOS Safari backgrounds the tab (lock
    the phone, switch apps while the window rolls down) — its JS timers just
    stop. This runs the same verify_command_with_retries the automation engine
    already trusts, so the result reaches you over a channel iOS can't pause.
    """
    verified = await verify_command_with_retries(command, params)
    if verified is None:
        return  # not verifiable, or inconclusive after every retry — nothing to report
    label = t_(_COMMAND_LABELS.get(command, command))
    if verified:
        title, tags, priority = t_("{cmd} confirmed", cmd=label), ["white_check_mark", "car"], "default"
    else:
        title, tags, priority = t_("{cmd} not confirmed", cmd=label), ["warning", "car"], "high"
    await asyncio.to_thread(
        notify, kind="control_result", key=key, title=title,
        body=t_("Triggered from the dashboard."), priority=priority, tags=tags,
    )


@router.post("/control")
async def control(req: ControlRequest, background_tasks: BackgroundTasks) -> dict:
    """Fire one C3 write command (lock, windows, climate, …). Real car effect.

    For verifiable commands, also confirms in the background and notifies —
    see `_notify_control_result`.
    """
    res = await run_command(req.command, req.params)
    if res.get("ok") and is_verifiable(req.command):
        background_tasks.add_task(_notify_control_result, req.command, req.params or {}, uuid.uuid4().hex)
    return res


@router.get("/log")
def api_log(
    limit: int = Query(300, ge=1, le=2000),
    level: str = Query("INFO"),
    since_id: int = Query(0, ge=0),
) -> dict:
    """Recent app-activity log records (in-memory ring buffer) for the /log tab.

    `since_id` lets the page fetch only new lines for incremental refresh.
    """
    import logging as _logging

    from ..logbuffer import get_records

    lvl = _logging.getLevelName((level or "INFO").upper())
    if not isinstance(lvl, int):
        lvl = _logging.INFO
    recs = get_records(limit=limit, min_level=lvl, since_id=since_id)
    return {"records": recs, "last_id": recs[-1]["id"] if recs else since_id}


def _log_level_no(level: str | None) -> int:
    import logging as _logging

    lvl = _logging.getLevelName((level or "INFO").upper())
    return lvl if isinstance(lvl, int) else _logging.INFO


def _log_filter_stmt(level, q, logger, since_ts, before_ts):  # noqa: ANN001
    """Shared WHERE-builder for the log history + export endpoints."""
    stmt = select(LogEntry).where(LogEntry.levelno >= _log_level_no(level))
    # Boundary is INCLUSIVE (`<=`): same-microsecond ties at the page edge are
    # returned again and de-duplicated on the client by content key, so paging
    # never drops a line that shares the boundary timestamp.
    if before_ts is not None:
        stmt = stmt.where(LogEntry.ts <= datetime.fromtimestamp(before_ts, tz=UTC).replace(tzinfo=None))
    if since_ts is not None:
        stmt = stmt.where(LogEntry.ts >= datetime.fromtimestamp(since_ts, tz=UTC).replace(tzinfo=None))
    if logger:
        stmt = stmt.where(LogEntry.logger == logger)
    if q:
        stmt = stmt.where(LogEntry.msg.ilike(f"%{q}%"))
    return stmt


@router.get("/log/history")
def api_log_history(
    before_ts: float | None = Query(None),
    since_ts: float | None = Query(None),
    level: str = Query("INFO"),
    q: str | None = Query(None),
    logger: str | None = Query(None),
    limit: int = Query(500, ge=1, le=2000),
) -> dict:
    """Older activity-log records from the persisted `LogEntry` table, for the
    /log tab's "load older" paging and filtering. Filters: `level` (min),
    `q` (substring of message), `logger` (exact source), `since_ts`/`before_ts`
    (epoch-second window). Returns oldest-first to match the live feed's client
    store; `has_more` signals another page is available."""
    stmt = _log_filter_stmt(level, q, logger, since_ts, before_ts)
    stmt = stmt.order_by(LogEntry.ts.desc(), LogEntry.id.desc()).limit(limit)
    with Session(get_engine()) as s:
        rows = s.exec(stmt).all()
    recs = [
        {
            "id": r.id,
            "ts": r.ts.replace(tzinfo=UTC).timestamp(),  # stored naive-UTC
            "level": r.level,
            "levelno": r.levelno,
            "logger": r.logger,
            "msg": r.msg,
            "exc": r.exc,
        }
        for r in rows
    ]
    recs.reverse()  # DB gave newest-first; client store is oldest-first
    return {"records": recs, "has_more": len(rows) == limit}


@router.get("/log/loggers")
def api_log_loggers() -> dict:
    """Distinct logger/source names in the persisted log, for the filter
    dropdown. `value` is the exact stored name (for the filter query); `label`
    is the short display name with the `polestar_dash.` prefix stripped."""
    with Session(get_engine()) as s:
        rows = s.exec(select(LogEntry.logger).distinct()).all()
    out = [
        {"value": r, "label": (r or "").replace("polestar_dash.", "") or "app"}
        for r in rows
        if r
    ]
    out.sort(key=lambda o: o["label"])
    return {"loggers": out}


@router.get("/log/export")
def api_log_export(
    level: str = Query("INFO"),
    q: str | None = Query(None),
    logger: str | None = Query(None),
    since_ts: float | None = Query(None),
    before_ts: float | None = Query(None),
    limit: int = Query(100_000, ge=1, le=1_000_000),
) -> PlainTextResponse:
    """Plain-text export of the persisted log (chronological, oldest-first),
    honouring the same filters as the history view (`logger` is the exact
    stored name from /api/log/loggers). Downloadable from /log."""
    stmt = _log_filter_stmt(level, q, logger, since_ts, before_ts)
    stmt = stmt.order_by(LogEntry.ts.asc(), LogEntry.id.asc()).limit(limit)
    with Session(get_engine()) as s:
        rows = s.exec(stmt).all()
    lines = [
        f"{r.ts.isoformat(sep=' ', timespec='seconds')}  {r.level:<8} "
        f"{(r.logger or '').replace('polestar_dash.', '') or 'app':<22} {r.msg}"
        + (f"\n{r.exc}" if r.exc else "")
        for r in rows
    ]
    body = "\n".join(lines) + ("\n" if lines else "")
    return PlainTextResponse(
        body,
        headers={"Content-Disposition": "attachment; filename=polestar-log.txt"},
    )


@router.get("/soh-history")
def api_soh_history() -> dict:
    """Persisted battery State-of-Health trend for the Battery & Charging chart.
    Falls back to a single live point if nothing is stored yet (fresh deploy)."""
    with Session(get_engine()) as s:
        rows = list(
            s.exec(select(SohSnapshot).order_by(SohSnapshot.computed_at.asc())).all()
        )
    points = [
        {"t": r.computed_at.isoformat(), "soh": r.soh_pct, "kwh": r.implied_kwh}
        for r in rows
    ]
    if not points:
        from ..insights_extras import compute_soh

        live = compute_soh()
        if live.get("enough_data"):
            points = [{
                "t": utcnow().isoformat(),
                "soh": live["soh_pct"],
                "kwh": live["implied_kwh"],
            }]
    return {"points": points}


@router.get("/app-settings/alerts")
def app_settings_alerts() -> dict:
    """Per-alert config (enable + quiet-hour window) for the settings frame."""
    return {"alerts": all_alert_configs()}


@router.post("/app-settings/alert/{kind}")
def app_settings_set_alert(
    kind: str,
    enabled: bool | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None, alias="to"),
) -> dict:
    """Update one alert. `from`/`to` are local hours; empty string clears the
    window (fires any time); omit to leave a field unchanged."""
    if kind not in ALERT_DEFAULTS:
        raise HTTPException(status_code=404, detail=f"Unknown alert: {kind}")
    set_alert_config(kind, enabled=enabled, hour_from=from_, hour_to=to)
    return {"kind": kind, **get_alert_config(kind)}


@router.get("/app-settings/automations")
def app_settings_automations() -> dict:
    """Per-rule automation config (enabled + mode) for the settings frame."""
    from ..automations import all_automation_configs

    return {"automations": all_automation_configs()}


@router.post("/app-settings/automation/{rule_id}")
def app_settings_set_automation(
    rule_id: str,
    enabled: bool | None = Query(None),
    mode: str | None = Query(None),
    after: str | None = Query(None),
) -> dict:
    from ..app_settings import get_automation_config, set_automation_config
    from ..automations import rule_by_id

    rule = rule_by_id(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"Unknown automation: {rule_id}")
    if mode is not None and mode not in ("autonom", "vorschlag"):
        raise HTTPException(status_code=400, detail=f"Unknown mode: {mode}")
    if mode == "autonom" and not rule.allow_autonomous:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{rule_id} cannot run autonomously — no signal tells us whether "
                "the key is inside the car, so this rule is suggestion-only."
            ),
        )
    set_automation_config(rule_id, enabled=enabled, mode=mode, after=after)
    return {"id": rule_id, **get_automation_config(
        rule_id, default_enabled=rule.default_enabled, default_mode=rule.default_mode,
        default_after=rule.default_after)}


@router.get("/app-settings/integrations")
def app_settings_integrations() -> dict:
    """Per-channel notification toggle (ntfy/email/telegram/whatsapp/mqtt)."""
    from ..app_settings import all_integration_configs

    return {"integrations": all_integration_configs()}


@router.post("/app-settings/integration/{integration_id}")
def app_settings_set_integration(
    integration_id: str,
    enabled: bool = Query(...),
) -> dict:
    from ..app_settings import (
        INTEGRATIONS,
        all_integration_configs,
        set_integration_enabled,
    )

    if integration_id not in INTEGRATIONS:
        raise HTTPException(status_code=404, detail=f"Unknown integration: {integration_id}")
    set_integration_enabled(integration_id, enabled)
    return next(c for c in all_integration_configs() if c["id"] == integration_id)


@router.post("/confirm/{token}")
async def confirm_action(token: str) -> dict:
    """Run a deferred (suggested) automation command after one-tap confirm.

    Atomically consumes the token, runs the command and verifies it. A second
    tap (or an expired token) gets 409 rather than re-firing the command."""
    from ..pending_actions import consume_pending, finalize_pending

    pa = consume_pending(token)
    if pa is None:
        raise HTTPException(status_code=409, detail="expired or already handled")
    try:
        params = json.loads(pa.params_json or "{}")
    except ValueError:
        params = {}
    res = await run_command(pa.command, params)
    # Wait for the car to actuate before judging (windows ~10 s), like the
    # autonomous path — avoids a false "could not confirm" on a command that
    # actually worked.
    verified = await verify_command_with_retries(pa.command, params)
    out = {"command": pa.command, **res, "verify": {"verified": verified}}
    finalize_pending(token, out)
    return out


@router.get("/control/verify")
async def control_verify(command: str, params: str | None = None) -> dict:
    """Force one fresh C3 read and report whether `command` took effect.

    `params` is an optional JSON string (only `set_amp_limit` needs it). Returns
    {verified: bool|None, …}; None = sent but not (yet) confirmable.
    """
    p: dict = {}
    if params:
        try:
            parsed = json.loads(params)
            if isinstance(parsed, dict):
                p = parsed
        except ValueError:
            p = {}
    return await verify_command(command, p)


def _recalc_session_energy() -> None:
    """Recompute stored energy for every complete session.

    Mirrors the close-time path in ``sessions.py``: integrated charging power
    when the session has power samples (cadence- and SoC-quantization-free,
    matches the operator's billed AC kWh), else the ΔSoC × capacity × loss
    estimate. Keeps History consistent after the calibrated capacity or loss
    factor changes.
    """
    from ..app_settings import get_calibrated_battery_kwh
    from ..sessions import (
        DEFAULT_BATTERY_KWH,
        _stamp_c3_env,
        compute_session_energy_kwh,
    )
    capacity = get_calibrated_battery_kwh(fallback=DEFAULT_BATTERY_KWH)
    with Session(get_engine()) as s:
        rows = list(s.exec(
            select(ChargeSession).where(ChargeSession.is_complete.is_(True))
        ).all())
        for cs in rows:
            energy = compute_session_energy_kwh(s, cs, capacity)
            if energy is not None:
                cs.energy_kwh = energy
                s.add(cs)
            # Backfill GPS/temp/PM2.5 for sessions that closed before C3
            # stamping existed (only fills empties; needs C3 history overlap).
            if cs.lat is None or cs.outside_temp_c is None or cs.air_pm25 is None:
                _stamp_c3_env(cs, s)
                s.add(cs)
        s.commit()


@router.get("/insights")
def insights(days: int = Query(90, ge=7, le=365), vin: str | None = None) -> dict:
    """Trip-level efficiency + cost-per-km bundle for the /insights page."""
    trips = detect_trips(vin=vin, days=days)
    default_price = get_default_price_ct()

    # Scatter: wh_per_km vs avg speed for each trip (avg speed = km / hours).
    scatter = []
    weekday_bucket: dict[int, list[int]] = {i: [] for i in range(7)}
    hour_bucket: dict[int, list[int]] = {h: [] for h in range(24)}
    for t in trips:
        wh = t.wh_per_km
        if wh is None:
            continue
        hours = max(t.duration_min, 1) / 60.0
        kmh = round(t.km / hours, 1) if hours > 0 else None
        scatter.append({
            "started_at": t.started_at.isoformat(),
            "km": t.km,
            "duration_min": t.duration_min,
            "avg_kmh": kmh,
            "wh_per_km": wh,
        })
        weekday_bucket[t.started_at.weekday()].append(wh)
        hour_bucket[t.started_at.hour].append(wh)

    def _avg(xs: list[int]) -> int | None:
        return int(sum(xs) / len(xs)) if xs else None

    weekday = [
        {"day": d, "avg_wh_per_km": _avg(weekday_bucket[d]), "n": len(weekday_bucket[d])}
        for d in range(7)
    ]
    by_hour = [
        {"hour": h, "avg_wh_per_km": _avg(hour_bucket[h]), "n": len(hour_bucket[h])}
        for h in range(24)
    ]

    # Cost per km: aggregate by ISO-week. For each completed charge session,
    # bill its energy at the effective price; for each trip, count its km.
    since = utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        stmt = (
            select(ChargeSession)
            .where(ChargeSession.started_at >= since)
            .where(ChargeSession.is_complete.is_(True))  # noqa: E712
        )
        if vin:
            stmt = stmt.where(ChargeSession.vin == vin)
        sessions = s.exec(stmt).all()

    cost_by_week: dict[str, dict] = {}
    for cs in sessions:
        wk = cs.started_at.strftime("%G-W%V")
        b = cost_by_week.setdefault(wk, {"week": wk, "kwh": 0.0, "cost_eur": 0.0, "km": 0.0})
        kwh = cs.energy_kwh or 0
        price = cs.price_ct_per_kwh if cs.price_ct_per_kwh is not None else default_price
        b["kwh"] += kwh
        b["cost_eur"] += kwh * price / 100.0
    for t in trips:
        wk = t.started_at.strftime("%G-W%V")
        b = cost_by_week.setdefault(wk, {"week": wk, "kwh": 0.0, "cost_eur": 0.0, "km": 0.0})
        b["km"] += t.km

    cost_rows = sorted(cost_by_week.values(), key=lambda r: r["week"])
    for r in cost_rows:
        r["kwh"] = round(r["kwh"], 1)
        r["cost_eur"] = round(r["cost_eur"], 2)
        r["km"] = round(r["km"], 1)
        r["eur_per_100km"] = round(r["cost_eur"] / r["km"] * 100, 2) if r["km"] > 0 else None

    # Compute the heavy pieces once, then feed them to the narrative engine so
    # it produces plain-language sentences without re-querying.
    forecast = compute_forecast(days=min(days, 30))
    temp_scatter = compute_temp_scatter(days=days)
    co2 = compute_co2(days=days)
    trip_cost = compute_trip_cost(days=days)
    charge_temp_scatter = compute_charge_temp_scatter(days=days)
    soh = compute_soh()
    try:
        narrative = build_narrative(
            days=days, vin=vin, forecast=forecast, trip_cost=trip_cost, co2=co2,
            soh=soh, scatter=scatter, temp_scatter=temp_scatter,
            charge_temp_scatter=charge_temp_scatter,
        )
    except Exception:  # noqa: BLE001 — a narrative glitch must not break the page
        log.exception("insights narrative build failed")
        narrative = {"summary": [], "cost": [], "efficiency": [], "battery": [],
                     "waste": [], "impact": []}

    return {
        "default_price_ct_per_kwh": default_price,
        "trips_total": len(scatter),
        "scatter": scatter,
        "weekday": weekday,
        "by_hour": by_hour,
        "cost_by_week": cost_rows,
        "forecast": forecast,
        "range_factor": compute_range_factor(days=days),
        "charge_heatmap": compute_charge_heatmap(days=365),
        "temp_scatter": temp_scatter,
        "co2": co2,
        "trip_cost": trip_cost,
        "battery_trend": compute_battery_trend(),
        "charge_temp_scatter": charge_temp_scatter,
        "air_exposure": compute_air_exposure(days=days),
        "c3_env_timeline": compute_c3_env_timeline(days=min(days, 60)),
        "soh": soh,
        "narrative": narrative,
    }


def _drain_dict(d) -> dict:
    return {
        "started_at": d.started_at.isoformat(),
        "ended_at": d.ended_at.isoformat(),
        "duration_min": d.duration_min,
        "soc_start": d.soc_start,
        "soc_end": d.soc_end,
        "soc_drop": d.soc_drop,
        "kwh": d.kwh,
        "implied_kw": d.implied_kw,
        "verdict": d.verdict,
        "kept": d.kept,
        "cause": d.cause,
        "parked_for_min": d.parked_for_min,
        "place": d.place,
        "lat": d.lat,
        "lng": d.lng,
    }


@router.get("/charging-cost")
def charging_cost(months: int = Query(12, ge=1, le=36), vin: str | None = None) -> dict:
    return compute_monthly_charging_cost(months=months, vin=vin)


@router.get("/battery-forecast")
def battery_forecast() -> dict:
    from ..battery_forecast import compute_degradation_forecast

    return compute_degradation_forecast()


@router.get("/idle-drain")
def idle_drain(days: int = Query(60, ge=1, le=365), vin: str | None = None) -> dict:
    diag = diagnose_idle_drains(vin=vin, days=days)
    candidates = diag["candidates"]  # already newest-first
    # `items` stays the kept-only list (unchanged shape for existing callers);
    # `diagnostics` exposes every candidate with its verdict so the reasoning —
    # what was kept vs excluded as a drive/settling/noise — is fully inspectable.
    items = [_drain_dict(d) for d in candidates if d.kept]
    total_kwh = round(sum(d.kwh for d in candidates if d.kept), 1)
    # The headline numbers now come from the aggregate baseline (parasitic
    # standby rate + a neutral pre-conditioning total), not the sparse,
    # weakly-attributed discrete events — which live on only in `diagnostics`.
    baseline = standby_baseline(vin=vin, days=days)
    return {
        "items": items,
        "total_kwh": total_kwh,
        "count": len(items),
        "standby": baseline["standby"],
        "preconditioning": baseline["preconditioning"],
        "battery_kwh": baseline["battery_kwh"],
        "diagnostics": [_drain_dict(d) for d in candidates],
        "tally": diag["tally"],
        "excluded": diag["excluded"],
    }


@router.get("/daily")
def daily(days: int = Query(30, ge=1, le=365), vin: str | None = None) -> list[dict]:
    """Per-day km driven (odometer max-min) and rough kWh from avg consumption."""
    since = (utcnow() - timedelta(days=days)).date()
    with Session(get_engine()) as s:
        day_col = func.date(VehicleSnapshot.fetched_at)
        stmt = (
            select(
                day_col.label("day"),
                func.min(VehicleSnapshot.odometer_km).label("odo_min"),
                func.max(VehicleSnapshot.odometer_km).label("odo_max"),
                func.avg(VehicleSnapshot.avg_consumption_kwh_per_100km).label("avg_cons"),
            )
            .where(VehicleSnapshot.fetched_at >= datetime.combine(since, datetime.min.time()))
            .where(VehicleSnapshot.odometer_km.is_not(None))
            .group_by(day_col)
            .order_by(day_col.asc())
        )
        if vin:
            stmt = stmt.where(VehicleSnapshot.vin == vin)
        rows = s.exec(stmt).all()

    out: list[dict] = []
    for r in rows:
        day_str, odo_min, odo_max, avg_cons = r
        km = None
        if odo_min is not None and odo_max is not None:
            km = round(max(0.0, odo_max - odo_min), 1)
        kwh = round(km * (avg_cons or 0) / 100, 1) if km else None
        out.append(
            {
                "day": str(day_str),
                "km": km,
                "avg_kwh_per_100km": round(avg_cons, 1) if avg_cons else None,
                "kwh_est": kwh,
            }
        )
    return out


@router.get("/tripbook.ics")
def tripbook_ics(
    days: int = Query(90, ge=1, le=730),
    vin: str | None = None,
) -> PlainTextResponse:
    trips = detect_trips(vin=vin, days=days)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//poledash//tripbook//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Polestar tripbook",
    ]
    def _ics_escape(text: str) -> str:
        # RFC 5545: backslash, semicolon, comma escaped; newline → \n
        return (
            text.replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\n", "\\n")
        )

    for t in trips:
        uid = f"{int(t.started_at.timestamp())}-{int(t.start_km)}@poledash"
        dtstart = t.started_at.strftime("%Y%m%dT%H%M%SZ")
        dtend = t.ended_at.strftime("%Y%m%dT%H%M%SZ")
        dtstamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
        eff = f", {t.kwh_per_100km} kWh/100km" if t.kwh_per_100km is not None else ""
        summary = _ics_escape(f"{t.km} km · {t.duration_min} min")
        desc_parts = [f"Odo {t.start_km:.0f} → {t.end_km:.0f} km{eff}"]
        if t.soc_start is not None and t.soc_end is not None:
            desc_parts.append(f"SoC {t.soc_start}% → {t.soc_end}%")
        desc = _ics_escape("\n".join(desc_parts))
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART:{dtstart}",
            f"DTEND:{dtend}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{desc}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    body = "\r\n".join(lines) + "\r\n"
    return PlainTextResponse(
        body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="tripbook.ics"'},
    )


@router.post("/journey-import")
async def journey_import(
    file: UploadFile = File(...),
    tz_offset_hours: float = Form(0.0),
    vin: str | None = Form(None),
) -> dict:
    """Import the Polestar app's journey-log CSV. Dedupes on (start, end, vin)."""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "expected a .csv file")
    max_size = 5 * 1024 * 1024
    chunks = []
    total = 0
    while chunk := await file.read(64 * 1024):
        total += len(chunk)
        if total > max_size:
            raise HTTPException(413, "file too large (>5 MB)")
        chunks.append(chunk)
    content = b"".join(chunks)
    try:
        records, parse_meta = parse_csv(content, tz_offset_hours=tz_offset_hours)
    except UnicodeDecodeError:
        raise HTTPException(400, "file is not UTF-8 text") from None
    except ValueError as e:
        # Unit detection refused to guess (or found implausible values) —
        # rejecting beats silently storing 1000×-wrong energies.
        raise HTTPException(400, str(e)) from None
    if not records:
        raise HTTPException(400, "no parseable rows — wrong format?")
    inserted, updated, skipped = upsert(records, vin=vin)
    calibrated = calibrate_battery_kwh()
    if calibrated is not None:
        from ..app_settings import CALIBRATED_BATTERY_KEY, set_setting
        set_setting(CALIBRATED_BATTERY_KEY, calibrated)
        # Keep existing sessions consistent with the new capacity.
        _recalc_session_energy()
    co2_tagged = tag_co2_for_imports()
    # On-car C3 measurements first (preferred), Open-Meteo as the fallback for
    # rows with no nearby C3 data (older trips).
    from ..c3_enrich import enrich_journey_imports_from_c3
    c3_enriched = enrich_journey_imports_from_c3(limit=500)
    weather = await enrich_weather_pending(limit=200)
    return {
        "parsed": len(records),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "calibrated_battery_kwh": calibrated,
        "co2_tagged": co2_tagged,
        "c3_enriched": c3_enriched,
        "weather": weather,
        "consumption_scaled": parse_meta["consumption_scaled"],
    }


@router.post("/journey-imports/enrich")
async def journey_imports_enrich() -> dict:
    """Re-run the post-import enrichments (weather, CO2). Safe to spam."""
    from ..c3_enrich import enrich_journey_imports_from_c3
    from ..co2 import backfill_charge_sessions

    co2_tagged_trips = tag_co2_for_imports()
    co2_tagged_sessions = backfill_charge_sessions()
    c3_enriched = enrich_journey_imports_from_c3(limit=1000)
    _recalc_session_energy()  # backfills session GPS/temp/PM2.5 too
    weather = await enrich_weather_pending(limit=500)
    return {
        "co2_tagged_trips": co2_tagged_trips,
        "co2_tagged_sessions": co2_tagged_sessions,
        "c3_enriched": c3_enriched,
        "weather": weather,
    }


@router.get("/trips")
def trips_api(days: int = Query(30, ge=1, le=365), vin: str | None = None) -> list[dict]:
    """Detected trips (odometer stream) enriched with the C3 GPS breadcrumb and
    average on-car temperature / cabin PM2.5 for each trip window. Powers the
    real-route layer on the tripbook map — works without any CSV upload."""
    from ..c3_enrich import c3_env_aggregate, c3_path_between

    out: list[dict] = []
    for t in detect_trips(vin=vin, days=days):
        path = c3_path_between(t.started_at, t.ended_at)
        env = c3_env_aggregate(t.started_at, t.ended_at)
        out.append({
            "started_at": t.started_at.isoformat(),
            "ended_at": t.ended_at.isoformat(),
            "km": t.km,
            "duration_min": t.duration_min,
            "kwh": t.kwh_used,
            "kwh_per_100km": t.kwh_per_100km,
            "start": [path[0]["lat"], path[0]["lng"]] if path else None,
            "end": [path[-1]["lat"], path[-1]["lng"]] if path else None,
            "path": [[p["lat"], p["lng"]] for p in path],
            "avg_temp_c": env["avg_temp_c"],
            "avg_pm25": env["avg_pm25"],
        })
    return out


@router.get("/route-stats")
def route_stats(
    days: int = Query(365, ge=1, le=3650),
    min_trips: int = Query(2, ge=1, le=20),
    radius_km: float = Query(1.0, ge=0.0, le=25.0),
) -> list[dict]:
    rows = list_routes(days=days, min_trips=min_trips, radius_km=radius_km)
    return [
        {**r, "last_at": r["last_at"].isoformat() if r.get("last_at") else None}
        for r in rows
    ]


@router.get("/route-trips")
def route_trips(
    start: str,
    end: str,
    days: int = Query(365, ge=1, le=3650),
    radius_km: float = Query(1.0, ge=0.0, le=25.0),
) -> list[dict]:
    return [j.model_dump(mode="json") for j in trips_for_route(start, end, days=days, radius_km=radius_km)]


@router.get("/address-labels")
def address_labels_get() -> dict[str, list[str]]:
    return get_address_labels()


@router.put("/address-labels")
def address_labels_put(payload: dict[str, list[str]]) -> dict[str, list[str]]:
    set_address_labels(payload)
    return get_address_labels()


@router.get("/address-geofences")
def address_geofences_get() -> dict[str, dict]:
    return get_address_geofences()


@router.put("/address-geofences")
def address_geofences_put(payload: dict[str, dict]) -> dict[str, dict]:
    """Radius-based labels: {label: {lat, lng, radius_m}}. Replaces the set."""
    set_address_geofences(payload)
    return get_address_geofences()


@router.post("/address-labels/assign")
def address_labels_assign(payload: dict) -> dict[str, list[str]]:
    """Assign a pattern (address string) to a label, removing it from any other.

    Body: {"pattern": "...", "label": "Heim"}. Empty/missing label unassigns.
    """
    pattern = str(payload.get("pattern") or payload.get("address") or "").strip()
    label = str(payload.get("label") or "").strip()
    if not pattern:
        raise HTTPException(400, "pattern required")
    labels = get_address_labels()
    for lbl in list(labels):
        labels[lbl] = [p for p in labels[lbl] if p != pattern]
        if not labels[lbl]:
            del labels[lbl]
    if label:
        labels.setdefault(label, []).append(pattern)
    set_address_labels(labels)
    return get_address_labels()


@router.get("/address-points")
def address_points(
    days: int = Query(365, ge=1, le=3650),
    vin: str | None = None,
) -> list[dict]:
    """Unique (address, lat, lng) points from JourneyImport with current label."""
    since = utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        stmt = select(JourneyImport).where(JourneyImport.started_at >= since)
        if vin is not None:
            stmt = stmt.where(JourneyImport.vin == vin)
        rows = s.exec(stmt).all()
    labels = get_address_labels()
    by_key: dict[tuple, dict] = {}
    for r in rows:
        for addr, lat, lng, ts in (
            (r.start_address, r.start_lat, r.start_lng, r.started_at),
            (r.end_address, r.end_lat, r.end_lng, r.ended_at),
        ):
            if not addr or lat is None or lng is None:
                continue
            key = (addr.strip(), round(lat, 4), round(lng, 4))
            e = by_key.get(key)
            if e is None:
                by_key[key] = {
                    "address": key[0],
                    "lat": key[1],
                    "lng": key[2],
                    "count": 1,
                    "last_seen": ts,
                    "label": label_for(key[0], labels),
                }
            else:
                e["count"] += 1
                if ts > e["last_seen"]:
                    e["last_seen"] = ts
    out = list(by_key.values())
    for e in out:
        e["last_seen"] = e["last_seen"].isoformat()
    out.sort(key=lambda x: x["count"], reverse=True)
    return out


@router.get("/trip-paths")
def trip_paths(
    days: int = Query(365, ge=1, le=3650),
    vin: str | None = None,
) -> list[dict]:
    """Per-trip start→end legs with metrics, for the Trip-Flow map.

    Only trips with both endpoints geocoded are returned. Labels resolve via
    the same string-pattern + geofence logic the route aggregator uses."""
    since = utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        stmt = (
            select(JourneyImport)
            .where(JourneyImport.started_at >= since)
            .order_by(JourneyImport.started_at)
        )
        if vin is not None:
            stmt = stmt.where(JourneyImport.vin == vin)
        rows = s.exec(stmt).all()
    labels = get_address_labels()
    geofences = get_address_geofences()
    price_ct, _ = blended_price_ct(days=max(days, 90))

    def _resolve(addr, lat, lng):
        return label_for(addr, labels) or geo_label_for(lat, lng, geofences)

    out: list[dict] = []
    for r in rows:
        if r.start_lat is None or r.start_lng is None or r.end_lat is None or r.end_lng is None:
            continue
        km = r.distance_km or 0
        kwh = r.consumption_kwh
        per100 = round(kwh * 100 / km, 1) if kwh and km > 0.5 else None
        co2_g = round(kwh * r.co2_g_per_kwh) if kwh and r.co2_g_per_kwh is not None else None
        out.append({
            "s": [r.start_lat, r.start_lng],
            "e": [r.end_lat, r.end_lng],
            "km": round(km, 1),
            "kwh": round(kwh, 2) if kwh is not None else None,
            "per100": per100,
            "co2_g": co2_g,
            "temp": r.outside_temp_c,
            "cost_eur": round(kwh * price_ct / 100, 2) if kwh is not None else None,
            "started_at": r.started_at.isoformat(),
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "start_label": _resolve(r.start_address, r.start_lat, r.start_lng),
            "end_label": _resolve(r.end_address, r.end_lat, r.end_lng),
            "start_addr": (r.start_address or "").split(",")[0],
            "end_addr": (r.end_address or "").split(",")[0],
        })
    return out


@router.get("/journey-imports")
def journey_imports(days: int = Query(30, ge=1, le=3650), vin: str | None = None) -> list[dict]:
    return [j.model_dump(mode="json") for j in list_imports(days=days, vin=vin)]


@router.get("/c3/latest")
async def c3_latest(vin: str | None = None, live: bool = False) -> dict:
    """Latest C3 state (doors/locks/climate/GPS) for the dashboard.

    With ``live=true`` the dashboard fetches fresh from the C3 backend (30 s
    cache, shared with /test) so it stays consistent with the Test page — a
    throttled/failed live read degrades gracefully to the persisted row. Without
    it, returns the persisted C3Snapshot (scheduler-kept, no backend hit) and
    only live-fetches when nothing has been polled yet, so the dashboard isn't
    blank before the first poll. Always carries the poll interval + source so
    the UI can show "last poll / interval"."""
    cfg = get_settings()
    meta = {
        "interval_min": cfg.c3_poll_interval_min,
        "fast_interval_min": cfg.c3_fast_poll_interval_min,
    }

    async def _live() -> dict | None:
        if not cfg.c3_enabled:
            return None
        data = await get_c3_snapshot()  # cached ~30 s; warm if /test was opened
        flat = c3_dict_to_row(data)
        if not flat:
            return None
        # A throttled/partial live read can come back `ok` but with the whole
        # exterior+location section empty (every door/lock/GPS field None).
        # Served over the persisted row that blanks the dashboard car: no
        # openings, no chip labels, and a false "all closed". Treat a stateless
        # read as a miss so we degrade to the DB row (the project conventions invariant).
        state_keys = ("is_locked", "door_fl", "door_fr", "door_rl", "door_rr",
                      "sunroof", "hood", "tailgate", "tank_lid", "lat")
        if all(flat.get(k) is None for k in state_keys):
            return None
        # trips (car odometers) + software (car-reported pending OTA) exist only
        # in the live snapshot, not the flat C3Snapshot row — attach for the
        # dashboard trip-meters line and the /software car-reported card.
        sw = data.get("software")
        if sw and sw.get("new_version"):
            from ..software import c3_software_seen

            fs = c3_software_seen(sw["new_version"])
            sw = {**sw, "first_seen": fs.isoformat() if fs else None}
        return {
            **flat, "fetched_at": utcnow().isoformat(), "source": "live",
            "trips": data.get("trips"), "software": sw, **meta,
        }

    # Live path (dashboard opt-in): prefer fresh data, fall back to the DB row.
    if live:
        fresh = await _live()
        if fresh is not None:
            return {**fresh, "connected": True}

    with Session(get_engine()) as s:
        base = select(C3Snapshot)
        if vin:
            base = base.where(C3Snapshot.vin == vin)
        latest = s.exec(
            base.order_by(C3Snapshot.fetched_at.desc()).limit(1)
        ).first()
        # When the C3 login degrades (throttle / lost car relation → re-pair),
        # every section comes back PERMISSION_DENIED and recent rows are all
        # `None`. Serve the last row that actually carried vehicle state so the
        # dashboard shows the last *known* state instead of a blank diagram, and
        # flag the disconnect so the UI can say "last state from HH:MM" rather
        # than pretend it's current. (the project conventions degrade-gracefully invariant.)
        good = s.exec(
            base.where(
                or_(
                    C3Snapshot.is_locked.is_not(None),
                    C3Snapshot.door_fl.is_not(None),
                    C3Snapshot.lat.is_not(None),
                )
            )
            .order_by(C3Snapshot.fetched_at.desc())
            .limit(1)
        ).first()
    row = good or latest
    if row is not None:
        # connected = the newest poll itself carried state (not a stale leftover).
        connected = latest is not None and row.id == latest.id
        out = {**row.model_dump(mode="json"), "source": "db",
               "connected": connected, **meta}
        if not connected and latest is not None:
            out["last_poll_at"] = latest.fetched_at.isoformat()
        return out
    # No persisted row yet — live-fetch so the dashboard isn't blank.
    fresh = await _live()
    return {**fresh, "connected": True} if fresh is not None else dict(meta)


@router.get("/weather/current")
async def weather_current(vin: str | None = None) -> dict:
    """Current weather at the car's latest known GPS (Open-Meteo, key-less).

    Small dashboard widget + powers the rain/windows alert. Returns {} when no
    GPS fix is on record yet."""
    from ..i18n import t
    from ..weather import current_at

    with Session(get_engine()) as s:
        stmt = (
            select(C3Snapshot.lat, C3Snapshot.lng)
            .where(C3Snapshot.lat.is_not(None))
            .order_by(C3Snapshot.fetched_at.desc())
        )
        if vin:
            stmt = stmt.where(C3Snapshot.vin == vin)
        row = s.exec(stmt.limit(1)).first()
    if not row or row[0] is None:
        return {}
    wx = await current_at(row[0], row[1])
    if wx is None:
        return {}
    labels = {
        "clear": t("Clear"), "cloudy": t("Cloudy"), "fog": t("Fog"),
        "drizzle": t("Drizzle"), "rain": t("Rain"), "snow": t("Snow"),
        "thunderstorm": t("Thunderstorm"), "unknown": t("unknown"),
    }
    wx["desc"] = labels.get(wx.get("bucket"), wx.get("bucket"))
    return wx


@router.get("/car-image")
def car_image_get():
    """Serve the user-uploaded vehicle image (stored in the data dir, never in
    the repo). 404 when none uploaded — the dashboard then falls back to
    CAR_IMAGE_URL or the built-in SVG."""
    from fastapi.responses import FileResponse, Response

    from ..app_settings import get_setting

    p = get_settings().data_dir / "car_image"
    if not p.exists():
        return Response(status_code=404)
    ct = get_setting("car_image_ct", "image/jpeg")
    return FileResponse(str(p), media_type=ct)


@router.post("/car-image")
async def car_image_upload(file: UploadFile = File(...)) -> dict:
    """Upload a vehicle image shown on the dashboard. Stored in the data dir."""
    from ..app_settings import set_setting

    allowed_types = {"image/jpeg", "image/png", "image/webp"}
    ct = (file.content_type or "").lower()
    if ct not in allowed_types:
        raise HTTPException(400, "expected a JPEG, PNG or WEBP image file")
    max_size = 5 * 1024 * 1024
    data = await file.read(max_size + 1)
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > max_size:
        raise HTTPException(413, "image too large (>5 MB)")
    p = get_settings().data_dir / "car_image"
    p.write_bytes(data)
    set_setting("car_image_ct", ct)
    return {"ok": True, "bytes": len(data), "content_type": ct}


@router.delete("/car-image")
def car_image_delete() -> dict:
    p = get_settings().data_dir / "car_image"
    existed = p.exists()
    if existed:
        p.unlink()
    return {"deleted": existed}


@router.get("/c3/track")
def c3_track(
    hours: int = Query(24, ge=1, le=720), vin: str | None = None
) -> list[dict]:
    """GPS trail from C3Snapshot history for the dashboard mini-map.

    Returns chronological [{lat, lng, ts, speed, gap_before}] points that have a
    fix. ``gap_before`` is True on the first point after a coverage gap longer
    than the outage threshold (a C3 outage, or simply the car parked off-grid) —
    the map breaks the polyline there instead of drawing a phantom straight line
    across an interval where we have no idea where the car went."""
    gap = disconnect_gap()
    since = utcnow() - timedelta(hours=hours)
    with Session(get_engine()) as s:
        stmt = (
            select(
                C3Snapshot.lat,
                C3Snapshot.lng,
                C3Snapshot.fetched_at,
                C3Snapshot.speed,
            )
            .where(C3Snapshot.fetched_at >= since)
            .where(C3Snapshot.lat.is_not(None))
            .where(C3Snapshot.lng.is_not(None))
            .order_by(C3Snapshot.fetched_at.asc())
        )
        if vin:
            stmt = stmt.where(C3Snapshot.vin == vin)
        rows = s.exec(stmt).all()
    out: list[dict] = []
    prev_ts = None
    for lat, lng, ts, speed in rows:
        gap_before = prev_ts is not None and (ts - prev_ts) > gap
        out.append({
            "lat": lat, "lng": lng, "ts": ts.isoformat(),
            "speed": speed, "gap_before": gap_before,
        })
        prev_ts = ts
    return out


@router.get("/c3/coverage")
def c3_coverage(days: int = Query(7, ge=1, le=90), vin: str | None = None) -> dict:
    """C3 connection-coverage summary for the diagnostics strip: the outage
    episodes (gaps between successive *stateful* polls longer than the disconnect
    threshold) over the window, plus a rough uptime ratio. All timestamps are the
    owner's local wall-clock so the strip reads in their time, not UTC."""
    gap_min = disconnect_gap()
    since = utcnow() - timedelta(days=days)
    state_cols = (
        C3Snapshot.is_locked, C3Snapshot.door_fl, C3Snapshot.sunroof,
        C3Snapshot.hood, C3Snapshot.tailgate, C3Snapshot.lat,
    )
    with Session(get_engine()) as s:
        stmt = (
            select(C3Snapshot.fetched_at)
            .where(C3Snapshot.fetched_at >= since)
            .where(or_(*[c.is_not(None) for c in state_cols]))
            .order_by(C3Snapshot.fetched_at.asc())
        )
        if vin:
            stmt = stmt.where(C3Snapshot.vin == vin)
        stamps = list(s.exec(stmt).all())
    outages: list[dict] = []
    down = timedelta()
    for a, b in zip(stamps, stamps[1:], strict=False):
        gap = b - a
        if gap > gap_min:
            down += gap
            outages.append({
                "start": fmt_local(a),
                "end": fmt_local(b),
                "minutes": round(gap.total_seconds() / 60),
            })
    span = (stamps[-1] - stamps[0]).total_seconds() if len(stamps) >= 2 else 0
    uptime = round(100 * (1 - down.total_seconds() / span), 1) if span else None
    return {
        "days": days,
        "last_seen": fmt_local(stamps[-1]) if stamps else None,
        "uptime_pct": uptime,
        "outages": list(reversed(outages)),  # newest first
    }


@router.get("/c3/field-history")
def c3_field_history(
    days: int = Query(30, ge=1, le=365), vin: str | None = None
) -> dict:
    """Time-series history of usage_mode, tyre pressures and TCU connectivity
    from C3Snapshot. Used by the Lab charts."""
    since = utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        stmt = (
            select(
                C3Snapshot.fetched_at,
                C3Snapshot.usage_mode,
                C3Snapshot.unavailable_reason,
                C3Snapshot.tyre_fl_kpa,
                C3Snapshot.tyre_fr_kpa,
                C3Snapshot.tyre_rl_kpa,
                C3Snapshot.tyre_rr_kpa,
                C3Snapshot.tcu_status,
                C3Snapshot.tcu_network,
                C3Snapshot.tcu_signal,
            )
            .where(C3Snapshot.fetched_at >= since)
            .order_by(C3Snapshot.fetched_at.asc())
        )
        if vin:
            stmt = stmt.where(C3Snapshot.vin == vin)
        rows = s.exec(stmt).all()
    points = []
    for ts, mode, reason, fl, fr, rl, rr, tcu_st, tcu_net, tcu_sig in rows:
        points.append({
            "ts": ts.isoformat(),
            "usage_mode": mode,
            "unavailable_reason": reason,
            "tyre_fl_kpa": fl,
            "tyre_fr_kpa": fr,
            "tyre_rl_kpa": rl,
            "tyre_rr_kpa": rr,
            "tcu_status": tcu_st,
            "tcu_network": tcu_net,
            "tcu_signal": tcu_sig,
        })
    return {"days": days, "points": points}


@router.delete("/journey-imports")
def journey_imports_clear(vin: str | None = None) -> dict:
    with Session(get_engine()) as s:
        stmt = select(JourneyImport)
        if vin is not None:
            stmt = stmt.where(JourneyImport.vin == vin)
        rows = s.exec(stmt).all()
        for r in rows:
            s.delete(r)
        s.commit()
    return {"deleted": len(rows)}


@router.get("/export.csv")
def export_csv(days: int = Query(365, ge=1, le=3650), vin: str | None = None) -> StreamingResponse:
    since = utcnow() - timedelta(days=days)

    def _generate():
        with Session(get_engine()) as s:
            stmt = (
                select(VehicleSnapshot)
                .where(VehicleSnapshot.fetched_at >= since)
                .order_by(VehicleSnapshot.fetched_at.asc())
            )
            if vin:
                stmt = stmt.where(VehicleSnapshot.vin == vin)
            cols = None
            for row in s.exec(stmt):
                buf = io.StringIO()
                writer = csv.writer(buf)
                d = row.model_dump()
                if cols is None:
                    cols = list(d.keys())
                    writer.writerow(cols)
                writer.writerow([d.get(c, "") for c in cols])
                yield buf.getvalue()

    filename = f"polestar-dash-{date.today().isoformat()}.csv"
    return StreamingResponse(
        _generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
