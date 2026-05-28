from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timedelta

from ..util import utcnow

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from ..config import get_settings
from ..db.models import ChargeSession, FetchError, JourneyImport
from ..db.session import get_engine
from ..journey_import import fleet_avg_kwh_per_km, list_imports
from ..routes_stats import list_routes
from ..templating import make_templates
from ..tripbook import detect_trips

router = APIRouter()
templates = make_templates()


def _charge_locations(days: int, vin: str | None, price_ct: float) -> list[dict]:
    """Cluster ChargeSessions by inferred location (~10 m) for the tripbook map.

    ChargeSessions have no coordinates of their own, so we attach a location by
    matching against JourneyImport rows: the closest trip *end* before the
    session start, or failing that the closest trip *start* after the session
    end. The car was parked at one of those points.
    """
    since = utcnow() - timedelta(days=days)
    window = timedelta(hours=4)
    with Session(get_engine()) as s:
        stmt = select(ChargeSession).where(ChargeSession.started_at >= since)
        if vin:
            stmt = stmt.where(ChargeSession.vin == vin)
        sessions = s.exec(stmt).all()
        # Pull a slightly wider import window so we can match sessions near the
        # edges of the requested range.
        imp_stmt = select(JourneyImport).where(
            JourneyImport.started_at >= since - timedelta(days=2)
        )
        if vin:
            imp_stmt = imp_stmt.where(JourneyImport.vin == vin)
        imports = s.exec(imp_stmt).all()

    # Pre-sort once and bisect on the timestamps — turns the per-session
    # match from O(n_imports) into O(log n_imports). Only the two neighbours
    # around the target time can be closest, so we just inspect those.
    ends_sorted = sorted(
        (j for j in imports if j.end_lat is not None and j.end_lng is not None),
        key=lambda j: j.ended_at,
    )
    end_keys = [j.ended_at for j in ends_sorted]
    starts_sorted = sorted(
        (j for j in imports if j.start_lat is not None and j.start_lng is not None),
        key=lambda j: j.started_at,
    )
    start_keys = [j.started_at for j in starts_sorted]

    def _nearest(keys: list[datetime], rows: list[JourneyImport], target: datetime):
        if not keys:
            return None
        i = bisect_left(keys, target)
        best = None
        best_diff = window
        for cand_i in (i - 1, i):
            if 0 <= cand_i < len(rows):
                diff = abs(keys[cand_i] - target)
                if diff <= best_diff:
                    best_diff = diff
                    best = rows[cand_i]
        return best

    def _locate(cs: ChargeSession) -> tuple[float, float, str | None] | None:
        j = _nearest(end_keys, ends_sorted, cs.started_at)
        if j is not None:
            return (j.end_lat, j.end_lng, j.end_address)
        ref = cs.ended_at or cs.started_at
        j = _nearest(start_keys, starts_sorted, ref)
        if j is not None:
            return (j.start_lat, j.start_lng, j.start_address)
        return None

    clusters: dict[tuple[float, float], dict] = {}
    for cs in sessions:
        loc = _locate(cs)
        if loc is None:
            continue
        lat, lng, addr = loc
        key = (round(lat, 4), round(lng, 4))  # ~11 m bucket
        eff_price = cs.price_ct_per_kwh if cs.price_ct_per_kwh is not None else price_ct
        kwh = cs.energy_kwh or 0
        c = clusters.get(key)
        if c is None:
            c = {
                "lat": lat,
                "lng": lng,
                "address": (addr or "").split(",")[0] if addr else None,
                "count": 0,
                "kwh": 0.0,
                "cost_eur": 0.0,
                "first_at": cs.started_at,
                "last_at": cs.started_at,
                "sessions": [],
            }
            clusters[key] = c
        c["count"] += 1
        c["kwh"] += kwh
        c["cost_eur"] += kwh * eff_price / 100.0
        if cs.started_at < c["first_at"]:
            c["first_at"] = cs.started_at
        if cs.started_at > c["last_at"]:
            c["last_at"] = cs.started_at
        c["sessions"].append(
            {
                "id": cs.id,
                "at": cs.started_at.isoformat(),
                "kwh": round(kwh, 1),
                "soc_start": cs.soc_start,
                "soc_end": cs.soc_end,
            }
        )

    for c in clusters.values():
        c["kwh"] = round(c["kwh"], 1)
        c["cost_eur"] = round(c["cost_eur"], 2)
        c["first_at"] = c["first_at"].isoformat()
        c["last_at"] = c["last_at"].isoformat()
        # Keep most-recent first inside the popup.
        c["sessions"].sort(key=lambda r: r["at"], reverse=True)
    return list(clusters.values())


@router.get("/tripbook", response_class=HTMLResponse)
def tripbook_page(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    vin: str | None = None,
):
    from datetime import timedelta

    from ..app_settings import blended_price_ct, get_calibrated_battery_kwh

    trips = detect_trips(vin=vin, days=days)
    price_ct, price_source = blended_price_ct(days=max(days, 90))
    battery_kwh = get_calibrated_battery_kwh()
    imports = list_imports(days=days, vin=vin)

    # Pair each detected trip with the closest imported trip by start time.
    # When matched, prefer the imported trip's real kWh over our SoC-based
    # estimate everywhere (per-row, monthly, grand total).
    match_window = timedelta(minutes=30)
    imports_sorted = sorted(imports, key=lambda j: j.started_at)
    import_keys = [j.started_at for j in imports_sorted]
    paired = []
    for t in trips:
        best = None
        if import_keys:
            i = bisect_left(import_keys, t.started_at)
            best_diff = match_window
            for ci in (i - 1, i):
                if 0 <= ci < len(imports_sorted):
                    diff = abs(imports_sorted[ci].started_at - t.started_at)
                    if diff <= best_diff:
                        best_diff = diff
                        best = imports_sorted[ci]
        paired.append((t, best))

    kwh_per_km = fleet_avg_kwh_per_km()

    def best_kwh(t, j) -> float | None:
        if j is not None and j.consumption_kwh:
            return float(j.consumption_kwh)
        if t.kwh_used is not None:
            return t.kwh_used
        # SoC didn't tick down (short trip, < 1 % resolution). Estimate
        # from fleet-average kWh/km so the cost column isn't empty.
        if t.km > 0:
            return round(t.km * kwh_per_km, 2)
        return None

    # Decorate each trip with a displayed kWh/100km rate. This is always the
    # trip's own estimate (car avg-consumption x distance, fleet avg as a
    # last resort) so old and new rows read uniformly — the real per-trip
    # rate from the journey log is no longer shown in this column; its
    # absolute energy still appears in the "kWh (real)" column. `kwh_est`
    # (used only as the cost fallback) keeps preferring the real value.
    triples = []
    for t, j in paired:
        est_kwh = t.kwh_used
        if est_kwh is None and t.km > 0:
            est_kwh = round(t.km * kwh_per_km, 2)
        per100 = round(est_kwh * 100 / t.km, 1) if (est_kwh and t.km > 0.5) else None
        triples.append((t, j, {"kwh_est": best_kwh(t, j), "per100_est": per100}))

    grand_km = round(sum(t.km for t, _ in paired), 1)
    grand_min = sum(t.duration_min for t, _ in paired)
    grand_kwh = round(sum((best_kwh(t, j) or 0) for t, j in paired), 1)
    grand_cost = round(grand_kwh * price_ct / 100.0, 2)

    totals: dict[str, dict] = {}
    for t, j in paired:
        key = t.started_at.strftime("%Y-%m")
        v = totals.setdefault(key, {"trips": 0, "km": 0.0, "minutes": 0, "kwh": 0.0})
        v["trips"] += 1
        v["km"] += t.km
        v["minutes"] += t.duration_min
        kwh = best_kwh(t, j)
        if kwh is not None:
            v["kwh"] += kwh
    for v in totals.values():
        v["km"] = round(v["km"], 1)
        v["kwh"] = round(v["kwh"], 2)
        v["avg_kwh_per_100km"] = (
            round(v["kwh"] * 100 / v["km"], 1) if v["km"] > 0 and v["kwh"] > 0 else None
        )
        v["cost_eur"] = round(v["kwh"] * price_ct / 100.0, 2)

    charge_locations = _charge_locations(days=days, vin=vin, price_ct=price_ct)

    imp_km = round(sum((j.distance_km or 0) for j in imports), 1)
    imp_kwh = round(sum((j.consumption_kwh or 0) for j in imports), 2)
    imp_cost = round(imp_kwh * price_ct / 100.0, 2)
    imp_avg = round(imp_kwh * 100 / imp_km, 1) if imp_km > 0 else None

    return templates.TemplateResponse(
        request,
        "tripbook.html",
        {
            "paired": triples,
            "trips": trips,
            "totals": totals,
            "grand_km": grand_km,
            "grand_min": grand_min,
            "grand_kwh": grand_kwh,
            "grand_cost": grand_cost,
            "price_ct": price_ct,
            "price_source": price_source,
            "battery_kwh": battery_kwh,
            "days": days,
            "imports": imports,
            "imp_km": imp_km,
            "imp_kwh": imp_kwh,
            "imp_cost": imp_cost,
            "imp_avg_kwh_per_100km": imp_avg,
            "charge_locations": charge_locations,
        },
    )


@router.get("/routes", response_class=HTMLResponse)
def routes_page(
    request: Request,
    days: int = Query(365, ge=1, le=3650),
    min_trips: int = Query(1, ge=1, le=20),
    radius_km: float = Query(1.0, ge=0.0, le=25.0),
):
    routes = list_routes(days=days, min_trips=min_trips, radius_km=radius_km)
    return templates.TemplateResponse(
        request,
        "routes.html",
        {"routes": routes, "days": days, "min_trips": min_trips, "radius_km": radius_km},
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    from ..app_settings import get_charging_loss_factor, get_default_price_ct
    from ..journey_import import get_address_labels

    s = get_settings()
    ntfy_note = f"topic: {s.ntfy_topic}" if s.ntfy_topic else "set NTFY_TOPIC to enable"
    tg_note = (
        "ready" if s.telegram_chat_id else "set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID"
    )
    mqtt_note = f"{s.mqtt_host}:{s.mqtt_port}" if s.mqtt_host else "set MQTT_HOST"
    smtp_note = (
        f"{s.smtp_host}:{s.smtp_port} → {s.smtp_to}" if s.smtp_host and s.smtp_to
        else "set SMTP_HOST + SMTP_TO in .env"
    )
    integrations = [
        ("Email (SMTP)", bool(s.smtp_host and s.smtp_to), smtp_note),
        ("ntfy.sh push", bool(s.ntfy_topic), ntfy_note),
        ("Telegram", bool(s.telegram_bot_token and s.telegram_chat_id), tg_note),
        ("MQTT / Home Assistant", bool(s.mqtt_host), mqtt_note),
    ]
    if not s.demo_mode:
        integrations.append(("Prometheus /metrics", True, "scrape /metrics"))
    address_labels = get_address_labels()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "address_labels": address_labels,
            "s": s,
            "integrations": integrations,
            "default_price_ct": get_default_price_ct(),
            "charging_loss_factor": get_charging_loss_factor(),
        },
    )


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    return templates.TemplateResponse(request, "history.html", {})


@router.get("/insights", response_class=HTMLResponse)
def insights_page(request: Request):
    return templates.TemplateResponse(request, "insights.html", {})


@router.get("/software", response_class=HTMLResponse)
def software_page(request: Request, vin: str | None = None):
    from ..software import dashboard_state

    return templates.TemplateResponse(request, "software.html", dashboard_state(vin))


@router.get("/sessions")
def sessions_page():
    # Sessions live as a block on /history now; keep the URL working.
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/history#sessions", status_code=307)


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
def session_detail_page(request: Request, session_id: int):
    return templates.TemplateResponse(
        request, "session_detail.html", {"session_id": session_id}
    )


@router.get("/errors", response_class=HTMLResponse)
def errors_page(request: Request):
    since = utcnow() - timedelta(days=14)
    with Session(get_engine()) as session:
        stmt = (
            select(FetchError)
            .where(FetchError.occurred_at >= since)
            .order_by(FetchError.occurred_at.desc())
        )
        rows = session.exec(stmt).all()
    return templates.TemplateResponse(request, "errors.html", {"rows": rows})
