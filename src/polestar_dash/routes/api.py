from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, timedelta

from ..util import utcnow

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy import func
from sqlmodel import Session, select

from ..app_settings import (
    get_charging_loss_factor,
    get_default_price_ct,
    set_charging_loss_factor,
    set_default_price_ct,
)
from ..db.models import ChargeSession, ChargingStation, JourneyImport, VehicleSnapshot
from ..db.session import get_engine
from ..idle_drain import detect_idle_drains
from ..insights_extras import (
    compute_charge_heatmap,
    compute_forecast,
    compute_range_factor,
    compute_temp_scatter,
)
from ..journey_import import (
    calibrate_battery_kwh,
    get_address_labels,
    label_for,
    list_imports,
    parse_csv,
    set_address_labels,
    tag_co2_for_imports,
    upsert,
)
from ..routes_stats import list_routes, trips_for_route
from ..weather import enrich_pending as enrich_weather_pending
from ..tripbook import detect_trips


def _session_to_dict(cs: ChargeSession, default_price: float) -> dict:
    d = cs.model_dump(mode="json")
    price = cs.price_ct_per_kwh if cs.price_ct_per_kwh is not None else default_price
    energy = cs.energy_kwh or 0
    d["effective_price_ct_per_kwh"] = price
    d["price_source"] = "session" if cs.price_ct_per_kwh is not None else "default"
    d["cost_eur"] = round(energy * price / 100.0, 2) if energy else 0.0
    return d

router = APIRouter(prefix="/api", tags=["api"])

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
) -> dict:
    """Update price / note for one charging session. price=null clears the override.

    When a note is saved, the session's GPS location is looked up and a
    ChargingStation is created/updated so future sessions at the same spot
    (~500 m) auto-fill name + price.
    """
    from ..stations import locate_session, upsert_station

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

    return _session_to_dict(row, get_default_price_ct())


@router.get("/charging-stations")
def charging_stations_list() -> list[dict]:
    from ..stations import list_stations
    return [st.model_dump(mode="json") for st in list_stations()]


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
    _recalc_session_energy(value)
    return {"charging_loss_factor": get_charging_loss_factor()}


def _recalc_session_energy(loss_factor: float) -> None:
    from ..sessions import DEFAULT_BATTERY_KWH
    with Session(get_engine()) as s:
        rows = list(s.exec(
            select(ChargeSession).where(ChargeSession.is_complete.is_(True))
        ).all())
        for cs in rows:
            if cs.soc_start is not None and cs.soc_end is not None:
                delta = max(0, cs.soc_end - cs.soc_start)
                net_kwh = delta / 100 * DEFAULT_BATTERY_KWH
                cs.energy_kwh = round(net_kwh * loss_factor, 1)
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

    return {
        "default_price_ct_per_kwh": default_price,
        "trips_total": len(scatter),
        "scatter": scatter,
        "weekday": weekday,
        "by_hour": by_hour,
        "cost_by_week": cost_rows,
        "forecast": compute_forecast(days=min(days, 30)),
        "range_factor": compute_range_factor(days=days),
        "charge_heatmap": compute_charge_heatmap(days=365),
        "temp_scatter": compute_temp_scatter(days=days),
    }


@router.get("/idle-drain")
def idle_drain(days: int = Query(60, ge=1, le=365), vin: str | None = None) -> dict:
    drains = detect_idle_drains(vin=vin, days=days)
    items = [
        {
            "started_at": d.started_at.isoformat(),
            "ended_at": d.ended_at.isoformat(),
            "duration_min": d.duration_min,
            "soc_start": d.soc_start,
            "soc_end": d.soc_end,
            "soc_drop": d.soc_drop,
            "kwh": d.kwh,
        }
        for d in drains
    ]
    total_kwh = round(sum(d.kwh for d in drains), 1)
    return {"items": items, "total_kwh": total_kwh, "count": len(items)}


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
        records = parse_csv(content, tz_offset_hours=tz_offset_hours)
    except UnicodeDecodeError:
        raise HTTPException(400, "file is not UTF-8 text")
    if not records:
        raise HTTPException(400, "no parseable rows — wrong format?")
    inserted, skipped = upsert(records, vin=vin)
    calibrated = calibrate_battery_kwh()
    if calibrated is not None:
        from ..app_settings import CALIBRATED_BATTERY_KEY, set_setting
        set_setting(CALIBRATED_BATTERY_KEY, calibrated)
    co2_tagged = tag_co2_for_imports()
    weather = await enrich_weather_pending(limit=200)
    return {
        "parsed": len(records),
        "inserted": inserted,
        "skipped": skipped,
        "calibrated_battery_kwh": calibrated,
        "co2_tagged": co2_tagged,
        "weather": weather,
    }


@router.post("/journey-imports/enrich")
async def journey_imports_enrich() -> dict:
    """Re-run the post-import enrichments (weather, CO2). Safe to spam."""
    from ..co2 import backfill_charge_sessions

    co2_tagged_trips = tag_co2_for_imports()
    co2_tagged_sessions = backfill_charge_sessions()
    weather = await enrich_weather_pending(limit=500)
    return {
        "co2_tagged_trips": co2_tagged_trips,
        "co2_tagged_sessions": co2_tagged_sessions,
        "weather": weather,
    }


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


@router.get("/journey-imports")
def journey_imports(days: int = Query(30, ge=1, le=3650), vin: str | None = None) -> list[dict]:
    return [j.model_dump(mode="json") for j in list_imports(days=days, vin=vin)]


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
