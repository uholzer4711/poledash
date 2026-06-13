"""Tiny helpers shared across the package — no internal imports allowed."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

# App-wide local timezone for human-facing clock decisions (quiet hours, night
# throttling). Matches the hardcoded zone in c3_anomalies' overnight nags.
LOCAL_TZ = ZoneInfo("Europe/Vienna")


def utcnow() -> datetime:
    """Naive UTC datetime, replacing the deprecated ``datetime.utcnow()``."""
    return datetime.now(UTC).replace(tzinfo=None)


def local_now() -> datetime:
    """Current wall-clock time in the app's local timezone (Europe/Vienna)."""
    return datetime.now(LOCAL_TZ)


def to_local(dt: datetime | None) -> datetime | None:
    """Convert a naive-UTC datetime (the form we persist) to local wall-clock.
    Tz-aware inputs are honoured as-is. Returns None for None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(LOCAL_TZ)


def fmt_local(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format a stored (naive-UTC) datetime as the user's local time with a short
    zone label, e.g. ``2026-06-12 07:43 CEST``. Human-facing surfaces should use
    this — never raw UTC — so the clock always matches the owner's wall time."""
    loc = to_local(dt)
    if loc is None:
        return ""
    return f"{loc:{fmt}} {loc:%Z}"


def in_hour_window(hour: int, start: int, end: int) -> bool:
    """True if `hour` is in [start, end), wrapping midnight when start>end."""
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km between two lat/lng points."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def charge_electrical(
    power_w: int | None,
    voltage_v: int | None,
    current_a: int | None,
    *,
    c3_type: str | None = None,
    api_type: str | None = None,
) -> dict:
    """Best-effort AC/DC mode + phase count for a charge-session display.

    Two facts the dashboard has to paper over:

    * The Polestar gRPC ``charging_type`` enum mislabels ordinary 11 kW
      3-phase AC as ``"DC"``. The C3 (Volvo) backend reports it correctly, so
      C3 wins; physics is the fallback when C3 is absent.
    * The API never exposes the *phase count*, yet it falls straight out of the
      power: ``P ≈ phases × V × I`` where ``V`` is the per-phase
      (line-to-neutral, ~230 V) voltage the API reports. 11040 W / (230 V ×
      16 A) = 3.0 ⇒ 3-phase.

    Returns ``{"mode", "phases", "line_to_line_v"}``:

    * ``mode`` — "AC"/"DC"/None. Prefers ``c3_type``; else a low per-phase
      voltage (≤ 280 V) at ≤ 24 kW is AC, a high bus voltage / high power is DC.
    * ``phases`` — 1–3, AC only (for DC, P ≈ V·I so it would read 1). None
      unless mode is AC.
    * ``line_to_line_v`` — the √3·V wallbox nominal (≈ 400 V at 230 V/phase),
      only for 3-phase. Explains "why 230 V, not 400 V?" on the gauge.
    """
    mode: str | None = None
    c3 = str(c3_type).upper() if c3_type else None
    if c3 in ("AC", "DC"):
        mode = c3
    elif voltage_v and power_w:
        if voltage_v <= 280 and power_w <= 24000:
            mode = "AC"
        elif voltage_v >= 300 or power_w > 24000:
            mode = "DC"
    if mode is None and api_type:
        api = str(api_type).upper()
        mode = api if api in ("AC", "DC") else None

    phases: int | None = None
    line_to_line_v: int | None = None
    if mode == "AC" and power_w and voltage_v and current_a and voltage_v > 0 and current_a > 0:
        p = round(power_w / (voltage_v * current_a))
        if 1 <= p <= 3:
            phases = p
            if p == 3:
                line_to_line_v = round(voltage_v * math.sqrt(3) / 10) * 10
    return {"mode": mode, "phases": phases, "line_to_line_v": line_to_line_v}
