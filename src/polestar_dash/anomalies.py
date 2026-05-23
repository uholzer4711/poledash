"""Anomaly checks run after every poll. Fire ntfy + Telegram on first hit;
dedup via notify()'s NotificationLog so we don't spam if the condition persists.

Each check is intentionally conservative — false positives erode trust faster
than missing one event. Thresholds are tunable in settings later if needed."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlmodel import Session, select

from .db.models import VehicleSnapshot
from .db.session import get_engine
from .notify import notify
from .polestar.fetcher import Snapshot
from .telegram import telegram_send

log = logging.getLogger(__name__)

# Tunables — kept module-level for clarity; promote to settings.py if user asks.
SOC_DROP_PERCENT = 8         # |Δ SoC| between consecutive samples
CHARGING_POWER_COLLAPSE_W = 3000  # power fell below this from > 30 kW peak
RANGE_FACTOR_FLOOR_KMPP = 2.5   # km per % SoC — winter-cold floor is ~3 normally
LOOKBACK_HOURS = 6
LOW_SOC_REMINDER_THRESHOLD = 25  # %, nudge if not plugged in below this
LOW_SOC_REMINDER_HOUR_FROM = 17  # local-hour window when nudges are useful
LOW_SOC_REMINDER_HOUR_TO = 23


def _prev_snapshot(vin: str, before: Snapshot) -> VehicleSnapshot | None:
    with Session(get_engine()) as s:
        stmt = (
            select(VehicleSnapshot)
            .where(VehicleSnapshot.vin == vin)
            .where(VehicleSnapshot.fetched_at < before.fetched_at)
            .where(VehicleSnapshot.fetched_at >= before.fetched_at - timedelta(hours=LOOKBACK_HOURS))
            .order_by(VehicleSnapshot.fetched_at.desc())
        )
        return s.exec(stmt).first()


def _alert(kind: str, key: str, title: str, body: str, vin: str) -> None:
    """ntfy + Telegram in one shot, with the same idempotency key."""
    notify(kind=kind, key=key, title=title, body=body, vin=vin,
           priority="high", tags=["warning"])
    telegram_send(f"⚠️ <b>{title}</b>\n{body}")


def _check_soc_drop(snap: Snapshot, prev: VehicleSnapshot) -> None:
    if snap.soc_percent is None or prev.soc_percent is None:
        return
    drop = prev.soc_percent - snap.soc_percent
    if drop < SOC_DROP_PERCENT:
        return
    # If car was charging or just drove far, that's expected.
    if snap.charging_status in ("Charging", "Smart Charging"):
        return
    drove = (
        prev.odometer_km is not None
        and snap.odometer_km is not None
        and (snap.odometer_km - prev.odometer_km) > 1.0
    )
    if drove:
        return
    key = f"{snap.vin}-{snap.fetched_at.strftime('%Y%m%dT%H%M')}-socdrop"
    _alert(
        kind="anomaly_soc_drop",
        key=key,
        title=f"Unexpected SoC drop · −{drop}%",
        body=(
            f"Battery dropped {prev.soc_percent}% → {snap.soc_percent}% with no "
            f"driving and no charging since {prev.fetched_at:%H:%M}."
        ),
        vin=snap.vin,
    )


def _check_power_collapse(snap: Snapshot, prev: VehicleSnapshot) -> None:
    """During an active session the power tanks — common signs of a flaky
    public charger or the car throttling at high SoC. Only alert on big drops."""
    if snap.charging_status not in ("Charging", "Smart Charging"):
        return
    if prev.charging_status not in ("Charging", "Smart Charging"):
        return
    if snap.charging_power_w is None or prev.charging_power_w is None:
        return
    if prev.charging_power_w < 30_000:
        return  # only meaningful when we were on a fast charger
    if snap.charging_power_w >= CHARGING_POWER_COLLAPSE_W:
        return
    # SoC near target → expected throttling, not a fault.
    if snap.target_soc_percent and snap.soc_percent and snap.soc_percent >= snap.target_soc_percent - 5:
        return
    # One alert per session-day.
    day = snap.fetched_at.strftime("%Y%m%d")
    key = f"{snap.vin}-{day}-powercollapse"
    _alert(
        kind="anomaly_power_collapse",
        key=key,
        title="Charging power collapsed",
        body=(
            f"Power dropped from {prev.charging_power_w/1000:.1f} kW to "
            f"{snap.charging_power_w/1000:.1f} kW at SoC {snap.soc_percent}%. "
            "Possible charger fault or thermal throttling."
        ),
        vin=snap.vin,
    )


def _check_range_factor(snap: Snapshot) -> None:
    """Range per %-SoC dropped below the floor — e.g. severe cold or
    degradation. Daily dedup; informational rather than urgent."""
    if not snap.range_km or not snap.soc_percent:
        return
    factor = snap.range_km / snap.soc_percent
    if factor >= RANGE_FACTOR_FLOOR_KMPP:
        return
    day = snap.fetched_at.strftime("%Y-%m-%d")
    key = f"{snap.vin}-{day}-rangefactor"
    _alert(
        kind="anomaly_range_factor",
        key=key,
        title=f"Low range factor · {factor:.2f} km/%",
        body=(
            f"{snap.range_km} km at {snap.soc_percent}% — "
            f"below the {RANGE_FACTOR_FLOOR_KMPP} km/% threshold. "
            "Cold weather or check tyre pressure / drag."
        ),
        vin=snap.vin,
    )


def _check_low_soc_unplugged(snap: Snapshot) -> None:
    """Nudge the user during evening hours if SoC is low and the car is not
    plugged in. Daily dedup so it fires at most once per day per VIN."""
    if snap.soc_percent is None or snap.soc_percent > LOW_SOC_REMINDER_THRESHOLD:
        return
    if snap.charging_status in ("Charging", "Smart Charging", "Done"):
        return
    if snap.charger_connection_status == "Connected":
        return
    # Local-time hour window (Europe/Vienna). Avoid 3 am nags.
    try:
        from zoneinfo import ZoneInfo

        local = snap.fetched_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Europe/Vienna"))
    except Exception:  # noqa: BLE001
        local = snap.fetched_at
    if not (LOW_SOC_REMINDER_HOUR_FROM <= local.hour <= LOW_SOC_REMINDER_HOUR_TO):
        return
    day = local.strftime("%Y-%m-%d")
    key = f"{snap.vin}-{day}-lowsoc"
    _alert(
        kind="low_soc_unplugged",
        key=key,
        title=f"Low battery · {snap.soc_percent}% and not plugged in",
        body=(
            f"~{snap.range_km or '?'} km of range left. "
            f"Consider plugging in tonight."
        ),
        vin=snap.vin,
    )


def run_anomaly_checks(snap: Snapshot) -> None:
    prev = _prev_snapshot(snap.vin, snap)
    if prev is not None:
        _check_soc_drop(snap, prev)
        _check_power_collapse(snap, prev)
    _check_range_factor(snap)
    _check_low_soc_unplugged(snap)
