"""Anomaly checks run after every poll. Fire ntfy + Telegram on first hit;
dedup via notify()'s NotificationLog so we don't spam if the condition persists.

Each check is intentionally conservative — false positives erode trust faster
than missing one event. Thresholds are tunable in settings later if needed."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlmodel import Session, select

from .app_settings import alert_enabled, in_alert_window
from .db.models import VehicleSnapshot
from .db.session import get_engine
from .i18n import t
from .notify import notify
from .polestar.fetcher import Snapshot
from .util import utcnow

log = logging.getLogger(__name__)

# Tunables — kept module-level for clarity; promote to settings.py if user asks.
SOC_DROP_PERCENT = 8         # |Δ SoC| between consecutive samples
CHARGING_POWER_COLLAPSE_W = 3000  # power fell below this from > 30 kW peak
RANGE_FACTOR_FLOOR_KMPP = 2.5   # km per % SoC — winter-cold floor is ~3 normally
RANGE_FACTOR_WINDOW_MIN = 60    # median over this window kills 1-2 poll API spikes
RANGE_FACTOR_MIN_SAMPLES = 5    # need a stable window before trusting the median
LOOKBACK_HOURS = 6
LOW_SOC_REMINDER_THRESHOLD = 25  # %, nudge if not plugged in below this
# The local-hour window when nudges are useful is configured per-alert in
# settings (alert "low_soc_unplugged"), default 17→23.


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
    """Send alert via all configured channels (ntfy, email, Telegram)."""
    notify(kind=kind, key=key, title=title, body=body, vin=vin,
           priority="high", tags=["warning"])


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
    # Daily dedup (like the sibling checks): a persistent / recurring drop
    # should nudge once per day, not on every poll. The old per-minute key was
    # effectively unique each poll, so the dedup never actually suppressed.
    key = f"{snap.vin}-{snap.fetched_at.strftime('%Y-%m-%d')}-socdrop"
    _alert(
        kind="anomaly_soc_drop",
        key=key,
        title=t("Unexpected SoC drop · −{drop}%", drop=drop),
        body=t(
            "Battery dropped {prev}% → {cur}% with no driving and no "
            "charging since {since}.",
            prev=prev.soc_percent,
            cur=snap.soc_percent,
            since=f"{prev.fetched_at:%H:%M}",
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
        title=t("Charging power collapsed"),
        body=t(
            "Power dropped from {before:.1f} kW to {after:.1f} kW at SoC {soc}%. "
            "Possible charger fault or thermal throttling.",
            before=prev.charging_power_w / 1000,
            after=snap.charging_power_w / 1000,
            soc=snap.soc_percent,
        ),
        vin=snap.vin,
    )


def _recent_range_factors(vin: str, before: Snapshot, minutes: int) -> list[float]:
    """Per-sample km/% factors over the last `minutes`, excluding `before`
    itself (the caller appends the current reading). The Polestar API's
    `range_km` field occasionally returns a transient garbage value for a
    single poll, so a one-shot check produces only false positives — we work
    off the window instead."""
    with Session(get_engine()) as s:
        stmt = (
            select(VehicleSnapshot.range_km, VehicleSnapshot.soc_percent)
            .where(VehicleSnapshot.vin == vin)
            .where(VehicleSnapshot.fetched_at < before.fetched_at)
            .where(VehicleSnapshot.fetched_at >= before.fetched_at - timedelta(minutes=minutes))
            .where(VehicleSnapshot.range_km.is_not(None))
            .where(VehicleSnapshot.soc_percent > 0)
        )
        rows = s.exec(stmt).all()
    return [rng / soc for rng, soc in rows if rng and soc]


def _check_range_factor(snap: Snapshot) -> None:
    """Range per %-SoC dropped below the floor — e.g. severe cold or
    degradation. Median over a window so a 1-2 poll `range_km` API spike
    can't fire a false alert. Daily dedup; informational rather than urgent."""
    if not snap.range_km or not snap.soc_percent:
        return
    factors = _recent_range_factors(snap.vin, snap, RANGE_FACTOR_WINDOW_MIN)
    factors.append(snap.range_km / snap.soc_percent)
    # Need a stable window: a lone reading (e.g. just after wake) isn't enough
    # to distinguish a real condition from API noise.
    if len(factors) < RANGE_FACTOR_MIN_SAMPLES:
        return
    factors.sort()
    median = factors[len(factors) // 2]
    if median >= RANGE_FACTOR_FLOOR_KMPP:
        return
    day = snap.fetched_at.strftime("%Y-%m-%d")
    key = f"{snap.vin}-{day}-rangefactor"
    _alert(
        kind="anomaly_range_factor",
        key=key,
        title=t("Low range factor · {factor:.2f} km/%", factor=median),
        body=t(
            "Median range per %-SoC is {factor:.2f} km/% over the last {win} min "
            "({n} samples) — below the {floor} km/% threshold. "
            "Possible causes: cold, tyre pressure or drag.",
            factor=median,
            win=RANGE_FACTOR_WINDOW_MIN,
            n=len(factors),
            floor=RANGE_FACTOR_FLOOR_KMPP,
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

        ts = snap.fetched_at
        # Only stamp UTC when the datetime is naive; otherwise we'd silently
        # clobber whatever timezone the caller already attached.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ZoneInfo("UTC"))
        local = ts.astimezone(ZoneInfo("Europe/Vienna"))
    except Exception:  # noqa: BLE001
        local = snap.fetched_at
    if not in_alert_window("low_soc_unplugged", local.hour):
        return
    day = local.strftime("%Y-%m-%d")
    key = f"{snap.vin}-{day}-lowsoc"
    _alert(
        kind="low_soc_unplugged",
        key=key,
        title=t("Low battery · {soc}% and not plugged in", soc=snap.soc_percent),
        body=t(
            "~{range} km of range left. Consider plugging in tonight.",
            range=snap.range_km or "?",
        ),
        vin=snap.vin,
    )


def check_battery_trend() -> None:
    """Weekly check: compare 4-week vs 12-week range factor average.

    If the recent average dropped >10%, fire an alert. Called by the
    weekly-digest scheduler job."""
    if not alert_enabled("battery_trend"):
        return
    from datetime import timedelta as td

    now = utcnow()
    with Session(get_engine()) as s:
        stmt = (
            select(VehicleSnapshot.fetched_at, VehicleSnapshot.range_km, VehicleSnapshot.soc_percent)
            .where(VehicleSnapshot.fetched_at >= now - td(weeks=12))
            .where(VehicleSnapshot.range_km.is_not(None))
            .where(VehicleSnapshot.soc_percent.is_not(None))
        )
        rows = list(s.exec(stmt).all())

    cutoff_4w = now - td(weeks=4)
    recent, baseline = [], []
    for ts, range_km, soc in rows:
        if not soc or soc <= 0 or not range_km:
            continue
        factor = range_km / (soc / 100)
        if ts >= cutoff_4w:
            recent.append(factor)
        else:
            baseline.append(factor)

    if len(recent) < 10 or len(baseline) < 10:
        return

    avg_recent = sum(recent) / len(recent)
    avg_baseline = sum(baseline) / len(baseline)
    if avg_baseline <= 0:
        return

    drop_pct = (avg_baseline - avg_recent) / avg_baseline * 100
    if drop_pct < 10:
        return

    week_key = now.strftime("%Y-W%W")
    _alert(
        kind="battery_trend",
        key=f"battery-trend-{week_key}",
        title=f"Range-Faktor sinkt: −{drop_pct:.0f}%",
        body=(
            f"4-Wochen-Schnitt: {avg_recent:.0f} km bei 100%\n"
            f"12-Wochen-Schnitt: {avg_baseline:.0f} km bei 100%\n"
            f"Mögliche Ursachen: Kälte, Reifendruck, Degradation."
        ),
        vin="",
    )


def run_anomaly_checks(snap: Snapshot, *, prev: VehicleSnapshot | None = None) -> None:
    # Caller (scheduler.poll_once) can pass `prev` to avoid re-running the
    # same indexed lookup that update_session already did. Falls back to a
    # local read for any other entry point.
    if prev is None:
        prev = _prev_snapshot(snap.vin, snap)
    if prev is not None:
        if alert_enabled("anomaly_soc_drop"):
            _check_soc_drop(snap, prev)
        if alert_enabled("anomaly_power_collapse"):
            _check_power_collapse(snap, prev)
    if alert_enabled("anomaly_range_factor"):
        _check_range_factor(snap)
    if alert_enabled("low_soc_unplugged"):
        _check_low_soc_unplugged(snap)
