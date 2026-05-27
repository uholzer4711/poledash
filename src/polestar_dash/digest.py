"""Weekly digest: summarise driving + charging stats and send via notifications."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlmodel import Session, select

from .app_settings import blended_price_ct
from .db.models import ChargeSession, VehicleSnapshot
from .db.session import get_engine
from .notify import notify
from .tripbook import detect_trips
from .util import utcnow

log = logging.getLogger(__name__)


def _week_sessions() -> list[ChargeSession]:
    since = utcnow() - timedelta(days=7)
    with Session(get_engine()) as s:
        stmt = (
            select(ChargeSession)
            .where(ChargeSession.is_complete.is_(True))
            .where(ChargeSession.started_at >= since)
        )
        return list(s.exec(stmt).all())


def _week_range_factor() -> float | None:
    since = utcnow() - timedelta(days=7)
    with Session(get_engine()) as s:
        stmt = (
            select(VehicleSnapshot.range_km, VehicleSnapshot.soc_percent)
            .where(VehicleSnapshot.fetched_at >= since)
            .where(VehicleSnapshot.range_km.is_not(None))
            .where(VehicleSnapshot.soc_percent.is_not(None))
        )
        rows = list(s.exec(stmt).all())
    factors = [r / (soc / 100) for r, soc in rows if soc and soc > 0 and r]
    return round(sum(factors) / len(factors), 1) if factors else None


def weekly_digest() -> None:
    """Build and send the weekly summary via all configured channels."""
    trips = detect_trips(days=7)
    sessions = _week_sessions()

    total_km = round(sum(t.km for t in trips), 1)
    total_trips = len(trips)
    total_kwh_driven = round(sum((t.kwh_used or 0) for t in trips), 1)

    total_sessions = len(sessions)
    total_kwh_charged = round(sum((cs.energy_kwh or 0) for cs in sessions), 1)

    price_ct, _ = blended_price_ct(days=90)
    total_cost = round(total_kwh_charged * price_ct / 100.0, 2)

    avg_eff = None
    if total_km > 0 and total_kwh_driven > 0:
        avg_eff = round(total_kwh_driven * 100 / total_km, 1)

    range_factor = _week_range_factor()

    if total_trips == 0 and total_sessions == 0:
        log.info("Weekly digest: no activity, skipping")
        return

    title = f"Wochenreport: {total_km} km, {total_kwh_charged} kWh, EUR {total_cost:.2f}"

    plain_lines = [
        f"Fahrten: {total_trips} ({total_km} km)",
    ]
    if avg_eff:
        plain_lines.append(f"Verbrauch: {avg_eff} kWh/100km")
    plain_lines.append(f"Ladesessions: {total_sessions} ({total_kwh_charged} kWh)")
    plain_lines.append(f"Kosten: EUR {total_cost:.2f} (@ {price_ct:.1f} ct/kWh)")
    if range_factor:
        plain_lines.append(f"Range-Faktor: {range_factor} km bei 100%")
    plain_body = "\n".join(plain_lines)

    html_lines = ["<b>Wochenreport</b><br><br>"]
    html_lines.append(f"<b>{total_trips}</b> Fahrten &middot; <b>{total_km}</b> km<br>")
    if avg_eff:
        html_lines.append(f"Verbrauch: <b>{avg_eff}</b> kWh/100km<br>")
    html_lines.append(f"<br><b>{total_sessions}</b> Ladesessions &middot; <b>{total_kwh_charged}</b> kWh<br>")
    html_lines.append(f"Kosten: <b>&euro; {total_cost:.2f}</b> (@ {price_ct:.1f} ct/kWh)<br>")
    if range_factor:
        html_lines.append(f"Range-Faktor: <b>{range_factor}</b> km bei 100%<br>")
    html_body = "".join(html_lines)

    week_key = utcnow().strftime("%Y-W%W")

    notify(
        kind="weekly_digest",
        key=week_key,
        title=title,
        body=plain_body,
        priority="low",
        tags=["chart_with_upwards_trend"],
    )

    log.info("Weekly digest sent: %s km, %s kWh, EUR %s", total_km, total_kwh_charged, total_cost)

    try:
        from .anomalies import check_battery_trend
        check_battery_trend()
    except Exception:  # noqa: BLE001
        log.exception("Battery trend check failed")
