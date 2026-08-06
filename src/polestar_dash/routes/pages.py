from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timedelta

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
from ..util import utcnow

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
    from ..app_settings import blended_price_ct, get_calibrated_battery_kwh

    trips = detect_trips(vin=vin, days=days)
    price_ct, price_source = blended_price_ct(days=max(days, 90))
    battery_kwh = get_calibrated_battery_kwh()
    imports = list_imports(days=days, vin=vin)

    kwh_per_km = fleet_avg_kwh_per_km()

    def trip_kwh(t) -> float | None:
        """Estimated energy for one detected trip, from the car's own average
        consumption (× distance), with the fleet average as a last resort.

        We deliberately do *not* fold the journey-log import into this figure.
        Detected trips and journey-log legs are cut differently — the SoC-gated
        detector merges stops into one trip, while the Polestar app logs each
        leg separately — so a long detected trip matches only one of its several
        import legs, and dividing that single leg's kWh over the full distance
        badly under-reports consumption (a 119 km trip read as 7 kWh/100km). The
        real per-leg numbers live in the separate "Journey log" section below;
        this column stays a clean, display-consistent estimate (≈ the car's
        own avg-consumption readout).
        """
        if t.kwh_used is not None:
            return t.kwh_used
        # SoC didn't tick down (short trip, < 1 % resolution). Estimate
        # from fleet-average kWh/km so the cost column isn't empty.
        if t.km > 0:
            return round(t.km * kwh_per_km, 2)
        return None

    triples = [(t, {"kwh_est": trip_kwh(t)}) for t in trips]

    grand_km = round(sum(t.km for t in trips), 1)
    grand_min = sum(t.duration_min for t in trips)
    grand_kwh = round(sum((trip_kwh(t) or 0) for t in trips), 1)
    grand_cost = round(grand_kwh * price_ct / 100.0, 2)

    totals: dict[str, dict] = {}
    for t in trips:
        key = t.started_at.strftime("%Y-%m")
        v = totals.setdefault(key, {"trips": 0, "km": 0.0, "minutes": 0, "kwh": 0.0})
        v["trips"] += 1
        v["km"] += t.km
        v["minutes"] += t.duration_min
        kwh = trip_kwh(t)
        if kwh is not None:
            v["kwh"] += kwh
    for v in totals.values():
        v["km"] = round(v["km"], 1)
        v["kwh"] = round(v["kwh"], 2)
        v["avg_kwh_per_100km"] = (
            round(v["kwh"] * 100 / v["km"], 1) if v["km"] > 0 and v["kwh"] > 0 else None
        )
        v["cost_eur"] = round(v["kwh"] * price_ct / 100.0, 2)

    # Group detected trips by month for the collapsible tripbook list — newest
    # month first, newest trip first within each — so a long window doesn't
    # render one endless table. Reuses the per-month `totals` computed above for
    # the summary shown on each (collapsed) month header.
    months_map: dict[str, list] = {}
    for pair in triples:
        months_map.setdefault(pair[0].started_at.strftime("%Y-%m"), []).append(pair)
    months = [
        {
            "key": key,
            "entries": sorted(
                months_map[key], key=lambda pe: pe[0].started_at, reverse=True
            ),
            "totals": totals.get(key),
        }
        for key in sorted(months_map, reverse=True)
    ]

    charge_locations = _charge_locations(days=days, vin=vin, price_ct=price_ct)

    imp_km = round(sum((j.distance_km or 0) for j in imports), 1)
    imp_kwh = round(sum((j.consumption_kwh or 0) for j in imports), 2)
    imp_cost = round(imp_kwh * price_ct / 100.0, 2)
    imp_avg = round(imp_kwh * 100 / imp_km, 1) if imp_km > 0 else None

    # Same month-accordion treatment for the journey log: a long window used
    # to render every imported leg as one endless flat table.
    imp_months_map: dict[str, list] = {}
    for j in imports:
        imp_months_map.setdefault(j.started_at.strftime("%Y-%m"), []).append(j)
    import_months = []
    for key in sorted(imp_months_map, reverse=True):
        entries = sorted(imp_months_map[key], key=lambda x: x.started_at, reverse=True)
        m_km = round(sum((j.distance_km or 0) for j in entries), 1)
        m_kwh = round(sum((j.consumption_kwh or 0) for j in entries), 2)
        import_months.append({
            "key": key,
            "entries": entries,
            "totals": {
                "trips": len(entries),
                "km": m_km,
                "kwh": m_kwh,
                "cost_eur": round(m_kwh * price_ct / 100.0, 2),
            },
        })

    # Odometer gap detection: between two consecutive imported trips the
    # start odometer of the later trip should equal the end odometer of the
    # earlier one. A positive jump means km were driven that the journey log
    # never recorded (a trip the app missed). The odometer is ground-truth,
    # so this is the most reliable "did the log miss anything?" signal we have.
    odo_gaps = []
    prev = None
    for j in sorted(imports, key=lambda x: x.started_at):
        if (
            prev is not None
            and prev.end_odometer_km is not None
            and j.start_odometer_km is not None
        ):
            gap = j.start_odometer_km - prev.end_odometer_km
            if gap >= 1:
                odo_gaps.append({
                    "from_at": prev.ended_at,
                    "to_at": j.started_at,
                    "from_addr": prev.end_address,
                    "to_addr": j.start_address,
                    "km": round(gap),
                })
        prev = j
    untracked_km = round(sum(g["km"] for g in odo_gaps))

    return templates.TemplateResponse(
        request,
        "tripbook.html",
        {
            "paired": triples,
            "months": months,
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
            "import_months": import_months,
            "imp_km": imp_km,
            "imp_kwh": imp_kwh,
            "imp_cost": imp_cost,
            "imp_avg_kwh_per_100km": imp_avg,
            "odo_gaps": odo_gaps,
            "untracked_km": untracked_km,
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


BROAD_PATTERN_THRESHOLD = 5  # a label pattern matching more distinct addresses is "too broad"


def _broad_label_warnings(labels: dict[str, list[str]]) -> dict[str, dict[str, int]]:
    """For each label pattern, count how many distinct imported addresses it
    matches (case-insensitive substring, same rule as ``label_for``). Patterns
    above the threshold collapse genuinely-different places into one label
    (e.g. a city/postcode pattern) — surfaced so the user can narrow them."""
    with Session(get_engine()) as s:
        rows = s.exec(
            select(JourneyImport.start_address, JourneyImport.end_address)
        ).all()
    addrs: set[str] = set()
    for start, end in rows:
        if start:
            addrs.add(start.lower())
        if end:
            addrs.add(end.lower())

    warnings: dict[str, dict[str, int]] = {}
    for label, patterns in labels.items():
        for pat in patterns:
            needle = pat.lower()
            if not needle:
                continue
            n = sum(1 for a in addrs if needle in a)
            if n > BROAD_PATTERN_THRESHOLD:
                warnings.setdefault(label, {})[pat] = n
    return warnings


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    from ..app_settings import (
        all_alert_configs,
        all_integration_configs,
        get_charging_loss_factor,
        get_default_price_ct,
        get_petrol_params,
    )
    from ..automations import all_automation_configs
    from ..journey_import import get_address_geofences, get_address_labels

    s = get_settings()
    notes = {
        "email": (
            f"{s.smtp_host}:{s.smtp_port} → {s.smtp_to}" if s.smtp_host and s.smtp_to
            else "set SMTP_HOST + SMTP_TO in .env"
        ),
        "ntfy": f"topic: {s.ntfy_topic}" if s.ntfy_topic else "set NTFY_TOPIC to enable",
        "telegram": (
            "ready" if s.telegram_chat_id else "set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID"
        ),
        "whatsapp": (
            "ready" if s.whatsapp_to else "set WHATSAPP_URL + WHATSAPP_API_KEY + WHATSAPP_TO"
        ),
        "mqtt": f"{s.mqtt_host}:{s.mqtt_port}" if s.mqtt_host else "set MQTT_HOST",
    }
    integrations = [
        {**cfg, "note": notes[cfg["id"]]} for cfg in all_integration_configs()
    ]
    if not s.demo_mode:
        integrations.append({
            "id": "prometheus", "label": "Prometheus /metrics",
            "configured": True, "enabled": True, "note": "scrape /metrics",
        })
    address_labels = get_address_labels()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "address_labels": address_labels,
            "address_geofences": get_address_geofences(),
            "label_warnings": _broad_label_warnings(address_labels),
            "s": s,
            "integrations": integrations,
            "default_price_ct": get_default_price_ct(),
            "charging_loss_factor": get_charging_loss_factor(),
            "petrol": get_petrol_params(),
            "alert_configs": all_alert_configs(),
            "automation_configs": all_automation_configs(),
        },
    )


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    return templates.TemplateResponse(request, "history.html", {})


@router.get("/spritmonitor", response_class=HTMLResponse)
def spritmonitor_page(request: Request):
    from ..app_settings import (
        get_spritmonitor_auto_export,
        get_spritmonitor_credentials,
        get_spritmonitor_lookup_ids,
        get_spritmonitor_vehicle,
    )

    return templates.TemplateResponse(
        request,
        "spritmonitor.html",
        {
            "sm_configured_creds": get_spritmonitor_credentials() is not None,
            "sm_vehicle": get_spritmonitor_vehicle(),
            "sm_lookup_ids": get_spritmonitor_lookup_ids(),
            "sm_auto_export": get_spritmonitor_auto_export(),
        },
    )


@router.get("/log", response_class=HTMLResponse)
def log_page(request: Request):
    """Live activity log — everything the app's own loggers emit."""
    return templates.TemplateResponse(request, "log.html", {})


@router.get("/confirm/{token}", response_class=HTMLResponse)
def confirm_page(request: Request, token: str):
    """One-tap confirmation landing page for an automation suggestion. Behind
    the same reverse-proxy auth as the rest of the app — the token just names
    which pending action to run."""
    from ..automations import rule_by_id
    from ..pending_actions import get_pending

    pa = get_pending(token)
    rule = rule_by_id(pa.rule_id) if pa else None
    return templates.TemplateResponse(
        request,
        "confirm.html",
        {"pa": pa, "token": token, "rule": rule},
    )


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
