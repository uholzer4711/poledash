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


def _latest_soc() -> tuple[int | None, int | None]:
    with Session(get_engine()) as s:
        stmt = (
            select(VehicleSnapshot.soc_percent, VehicleSnapshot.range_km)
            .where(VehicleSnapshot.soc_percent.is_not(None))
            .order_by(VehicleSnapshot.fetched_at.desc())
            .limit(1)
        )
        row = s.exec(stmt).first()
    return (row[0], row[1]) if row else (None, None)


def _build_html(
    total_trips: int,
    total_km: float,
    avg_eff: float | None,
    total_sessions: int,
    total_kwh_charged: float,
    total_cost: float,
    price_ct: float,
    range_factor: float | None,
    soc: int | None,
    range_km: int | None,
    sessions: list[ChargeSession],
) -> str:
    """Build a styled HTML email body."""

    def stat_card(label: str, value: str, unit: str = "") -> str:
        return (
            f'<td style="background:#1a1c20;padding:20px 24px;width:25%">'
            f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:10px;'
            f'text-transform:uppercase;letter-spacing:1px;color:#8a8d93;margin-bottom:6px">{label}</div>'
            f'<div style="font-family:\'Inter Tight\',sans-serif;font-size:32px;'
            f'font-weight:700;color:#e8e8ea;line-height:1">{value}'
            f'<span style="font-size:14px;color:#8a8d93;margin-left:4px">{unit}</span></div>'
            f'</td>'
        )

    soc_display = f"{soc}%" if soc is not None else "—"
    range_display = f"{range_km}" if range_km is not None else "—"
    eff_display = f"{avg_eff}" if avg_eff else "—"
    rf_display = f"{range_factor}" if range_factor else "—"

    session_rows = ""
    for cs in sessions[:5]:
        station = cs.note or "—"
        kwh = f"{cs.energy_kwh:.1f}" if cs.energy_kwh else "—"
        soc_range = f"{cs.soc_start} → {cs.soc_end}%" if cs.soc_start is not None else "—"
        eff_price = cs.price_ct_per_kwh if cs.price_ct_per_kwh is not None else price_ct
        cost = f"&euro; {(cs.energy_kwh or 0) * eff_price / 100:.2f}"
        session_rows += (
            f'<tr style="border-bottom:1px solid #2a2c30">'
            f'<td style="padding:8px 12px;color:#e8e8ea;font-size:13px">{station}</td>'
            f'<td style="padding:8px 12px;color:#8a8d93;font-size:13px">{soc_range}</td>'
            f'<td style="padding:8px 12px;color:#e8e8ea;font-size:13px;text-align:right">{kwh} kWh</td>'
            f'<td style="padding:8px 12px;color:#ff5b14;font-size:13px;text-align:right;font-weight:600">{cost}</td>'
            f'</tr>'
        )

    return f'''<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>
<body style="margin:0;padding:0;background:#0e0f11;font-family:'Inter Tight','Helvetica Neue',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0e0f11;padding:32px 16px">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%">

  <!-- Header -->
  <tr><td style="padding:0 0 24px">
    <table width="100%" cellpadding="0" cellspacing="0">
    <tr>
      <td style="font-family:'Inter Tight',sans-serif;font-size:24px;font-weight:700;color:#e8e8ea">
        poledash <span style="color:#ff5b14">Wochenreport</span>
      </td>
      <td align="right" style="font-family:'JetBrains Mono',monospace;font-size:11px;color:#8a8d93">
        KW {utcnow().isocalendar()[1]} / {utcnow().year}
      </td>
    </tr>
    </table>
    <div style="height:2px;background:#ff5b14;margin-top:12px;width:48px"></div>
  </td></tr>

  <!-- Current status -->
  <tr><td style="padding-bottom:16px">
    <div style="font-family:'JetBrains Mono',monospace;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#8a8d93;margin-bottom:8px">Aktuell</div>
    <table width="100%" cellpadding="0" cellspacing="4"><tr>
      {stat_card("SoC", soc_display)}
      {stat_card("Reichweite", range_display, "km")}
    </tr></table>
  </td></tr>

  <!-- Driving stats -->
  <tr><td style="padding-bottom:16px">
    <div style="font-family:'JetBrains Mono',monospace;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#8a8d93;margin-bottom:8px">Gefahren</div>
    <table width="100%" cellpadding="0" cellspacing="4"><tr>
      {stat_card("Fahrten", str(total_trips))}
      {stat_card("Kilometer", str(total_km), "km")}
      {stat_card("Effizienz", eff_display, "kWh/100km")}
      {stat_card("Range-Faktor", rf_display, "km@100%")}
    </tr></table>
  </td></tr>

  <!-- Charging stats -->
  <tr><td style="padding-bottom:16px">
    <div style="font-family:'JetBrains Mono',monospace;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#8a8d93;margin-bottom:8px">Geladen</div>
    <table width="100%" cellpadding="0" cellspacing="4"><tr>
      {stat_card("Sessions", str(total_sessions))}
      {stat_card("Energie", str(total_kwh_charged), "kWh")}
      {stat_card("Kosten", f"&euro; {total_cost:.2f}", "")}
      {stat_card("Preis", f"{price_ct:.1f}", "ct/kWh")}
    </tr></table>
  </td></tr>

  <!-- Session detail table -->
  {"" if not session_rows else f"""
  <tr><td style="padding-bottom:16px">
    <div style="font-family:'JetBrains Mono',monospace;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#8a8d93;margin-bottom:8px">Ladesessions</div>
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#1a1c20">
      <tr style="border-bottom:1px solid #2a2c30">
        <th style="padding:8px 12px;text-align:left;font-family:'JetBrains Mono',monospace;font-size:10px;color:#8a8d93;text-transform:uppercase;letter-spacing:1px">Station</th>
        <th style="padding:8px 12px;text-align:left;font-family:'JetBrains Mono',monospace;font-size:10px;color:#8a8d93;text-transform:uppercase;letter-spacing:1px">SoC</th>
        <th style="padding:8px 12px;text-align:right;font-family:'JetBrains Mono',monospace;font-size:10px;color:#8a8d93;text-transform:uppercase;letter-spacing:1px">kWh</th>
        <th style="padding:8px 12px;text-align:right;font-family:'JetBrains Mono',monospace;font-size:10px;color:#8a8d93;text-transform:uppercase;letter-spacing:1px">Kosten</th>
      </tr>
      {session_rows}
    </table>
  </td></tr>
  """}

  <!-- Footer -->
  <tr><td style="padding-top:16px;border-top:1px solid #2a2c30">
    <div style="font-family:'JetBrains Mono',monospace;font-size:10px;color:#8a8d93;text-align:center">
      poledash &middot; Polestar 2 Telemetry Dashboard
    </div>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>'''


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
    soc, range_km = _latest_soc()

    if total_trips == 0 and total_sessions == 0:
        log.info("Weekly digest: no activity, skipping")
        return

    title = f"Wochenreport KW{utcnow().isocalendar()[1]}: {total_km} km, {total_kwh_charged} kWh, EUR {total_cost:.2f}"

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

    html_body = _build_html(
        total_trips=total_trips,
        total_km=total_km,
        avg_eff=avg_eff,
        total_sessions=total_sessions,
        total_kwh_charged=total_kwh_charged,
        total_cost=total_cost,
        price_ct=price_ct,
        range_factor=range_factor,
        soc=soc,
        range_km=range_km,
        sessions=sessions,
    )

    iso_year, iso_week, _ = utcnow().isocalendar()
    week_key = f"{iso_year}-W{iso_week:02d}"

    # One deduped call drives all channels — the HTML email goes through the
    # same per-channel dedup as ntfy/Telegram, so a re-run in the same week
    # doesn't re-send the email (it previously bypassed dedup entirely).
    notify(
        kind="weekly_digest",
        key=week_key,
        title=title,
        body=plain_body,
        email_html=html_body,
        priority="low",
        tags=["chart_with_upwards_trend"],
    )

    log.info("Weekly digest sent: %s km, %s kWh, EUR %s", total_km, total_kwh_charged, total_cost)

    try:
        from .anomalies import check_battery_trend
        check_battery_trend()
    except Exception:  # noqa: BLE001
        log.exception("Battery trend check failed")
