from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from ..config import get_settings
from ..db.models import FetchError
from ..db.session import get_engine
from ..journey_import import fleet_avg_kwh_per_km, list_imports
from ..routes_stats import list_routes
from ..templating import make_templates
from ..tripbook import detect_trips

router = APIRouter()
templates = make_templates()


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
    paired = []
    for t in trips:
        best = None
        best_diff = match_window
        for j in imports:
            diff = abs(j.started_at - t.started_at)
            if diff <= best_diff:
                best_diff = diff
                best = j
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

    # Decorate each trip with an estimated kWh/100km that falls back to the
    # fleet avg when SoC drop is zero (short trips). The template reads it
    # via the third tuple element.
    triples = []
    for t, j in paired:
        kwh = best_kwh(t, j)
        per100 = round(kwh * 100 / t.km, 1) if (kwh and t.km > 0.5) else None
        triples.append((t, j, {"kwh_est": kwh, "per100_est": per100}))

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
        },
    )


@router.get("/routes", response_class=HTMLResponse)
def routes_page(
    request: Request,
    days: int = Query(365, ge=1, le=3650),
    min_trips: int = Query(1, ge=1, le=20),
):
    routes = list_routes(days=days, min_trips=min_trips)
    return templates.TemplateResponse(
        request,
        "routes.html",
        {"routes": routes, "days": days, "min_trips": min_trips},
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    from ..app_settings import get_default_price_ct
    from ..journey_import import get_address_labels

    s = get_settings()
    ntfy_note = f"topic: {s.ntfy_topic}" if s.ntfy_topic else "set NTFY_TOPIC to enable"
    tg_note = (
        "ready" if s.telegram_chat_id else "set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID"
    )
    mqtt_note = f"{s.mqtt_host}:{s.mqtt_port}" if s.mqtt_host else "set MQTT_HOST"
    integrations = [
        ("ntfy.sh push", bool(s.ntfy_topic), ntfy_note),
        ("Telegram", bool(s.telegram_bot_token and s.telegram_chat_id), tg_note),
        ("MQTT / Home Assistant", bool(s.mqtt_host), mqtt_note),
        ("Prometheus /metrics", True, "scrape /metrics"),
    ]
    address_labels = get_address_labels()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "address_labels": address_labels,
            "s": s,
            "integrations": integrations,
            "default_price_ct": get_default_price_ct(),
        },
    )


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    return templates.TemplateResponse(request, "history.html", {})


@router.get("/insights", response_class=HTMLResponse)
def insights_page(request: Request):
    return templates.TemplateResponse(request, "insights.html", {})


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
    since = datetime.utcnow() - timedelta(days=14)
    with Session(get_engine()) as session:
        stmt = (
            select(FetchError)
            .where(FetchError.occurred_at >= since)
            .order_by(FetchError.occurred_at.desc())
        )
        rows = session.exec(stmt).all()
    return templates.TemplateResponse(request, "errors.html", {"rows": rows})
