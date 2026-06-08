"""User-editable app settings, persisted in the AppSetting table.

`pydantic-settings` (config.py) holds the *environmental* configuration —
secrets, connection strings, polling cadences. This module is for values
the user wants to tweak from the web UI without restarting the container
(default electricity price, future thresholds, etc.).
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from .db.models import AppSetting
from .db.session import get_engine
from .util import utcnow

DEFAULT_PRICE_KEY = "default_price_ct_per_kwh"
DEFAULT_PRICE_FALLBACK = 50.0  # ct/kWh — user-requested starter


def get_setting(key: str, default: Any = None) -> Any:
    with Session(get_engine()) as s:
        row = s.exec(select(AppSetting).where(AppSetting.key == key)).first()
    return row.value if row else default


def set_setting(key: str, value: Any) -> None:
    str_val = str(value)
    with Session(get_engine()) as s:
        row = s.exec(select(AppSetting).where(AppSetting.key == key)).first()
        if row is None:
            s.add(AppSetting(key=key, value=str_val, updated_at=utcnow()))
        else:
            row.value = str_val
            row.updated_at = utcnow()
            s.add(row)
        s.commit()


def get_default_price_ct() -> float:
    raw = get_setting(DEFAULT_PRICE_KEY, DEFAULT_PRICE_FALLBACK)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_PRICE_FALLBACK


def set_default_price_ct(value: float) -> None:
    if value < 0 or value > 500:
        raise ValueError("Price must be between 0 and 500 ct/kWh")
    set_setting(DEFAULT_PRICE_KEY, round(float(value), 2))


CHARGING_LOSS_FACTOR_KEY = "charging_loss_factor"
CHARGING_LOSS_FACTOR_DEFAULT = 1.12  # ~12 % typical AC charging losses


def get_charging_loss_factor() -> float:
    raw = get_setting(CHARGING_LOSS_FACTOR_KEY, CHARGING_LOSS_FACTOR_DEFAULT)
    try:
        v = float(raw)
        return v if 1.0 <= v <= 1.5 else CHARGING_LOSS_FACTOR_DEFAULT
    except (TypeError, ValueError):
        return CHARGING_LOSS_FACTOR_DEFAULT


def set_charging_loss_factor(value: float) -> None:
    if value < 1.0 or value > 1.5:
        raise ValueError("Charging loss factor must be between 1.0 and 1.5")
    set_setting(CHARGING_LOSS_FACTOR_KEY, round(float(value), 3))


CALIBRATED_BATTERY_KEY = "calibrated_battery_kwh"


def get_calibrated_battery_kwh(fallback: float = 78.0) -> float:
    raw = get_setting(CALIBRATED_BATTERY_KEY, None)
    if raw is None:
        return fallback
    try:
        return float(raw)
    except (TypeError, ValueError):
        return fallback


# --- Alert configuration (per-alert enable + quiet-hour window) ---------------
# Each notification "kind" can be toggled and given a local-hour window in which
# it may fire. A window of (None, None) means "any time". Wrapping windows
# (from > to, e.g. 20→6) span midnight. Defaults reproduce the previously
# hardcoded behavior in anomalies.py / c3_anomalies.py.
#
# kind -> {enabled, from, to}. `from`/`to` are local hours (Europe/Vienna) or None.
ALERT_DEFAULTS: dict[str, dict] = {
    "c3_open": {"enabled": True, "from": 20, "to": 6},
    "c3_unlocked": {"enabled": True, "from": 20, "to": 6},
    "c3_tyre": {"enabled": True, "from": None, "to": None},
    "c3_light": {"enabled": True, "from": None, "to": None},
    "c3_level": {"enabled": True, "from": None, "to": None},
    "c3_rain_windows": {"enabled": True, "from": None, "to": None},
    "anomaly_soc_drop": {"enabled": True, "from": None, "to": None},
    "anomaly_power_collapse": {"enabled": True, "from": None, "to": None},
    "anomaly_range_factor": {"enabled": True, "from": None, "to": None},
    "low_soc_unplugged": {"enabled": True, "from": 17, "to": 23},
    "battery_trend": {"enabled": True, "from": None, "to": None},
}

# English label per kind (the i18n source string) — the settings UI wraps these
# in t(). Kept here so the catalog and the template stay in sync via one source.
ALERT_LABELS: dict[str, str] = {
    "c3_open": "Door / window left open",
    "c3_unlocked": "Car left unlocked",
    "c3_tyre": "Tyre warning",
    "c3_light": "Light failure",
    "c3_level": "Fluid / level warning",
    "c3_rain_windows": "Rain & windows open",
    "anomaly_soc_drop": "Unexpected SoC drop",
    "anomaly_power_collapse": "Charging power collapse",
    "anomaly_range_factor": "Low range factor",
    "low_soc_unplugged": "Low battery reminder",
    "battery_trend": "Battery trend",
}


def _as_bool(raw: Any, default: bool = False) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _as_hour(raw: Any, default: int | None) -> int | None:
    """Parse a stored hour. Empty string → None (no window); missing → default."""
    if raw is None:
        return default
    s = str(raw).strip()
    if s == "":
        return None
    try:
        return max(0, min(23, int(s)))
    except (TypeError, ValueError):
        return default


def _hour_str(v: Any) -> str:
    if v is None or v == "":
        return ""
    try:
        return str(max(0, min(23, int(v))))
    except (TypeError, ValueError):
        return ""


def _default_enabled(kind: str) -> bool:
    """Default enabled state — seeds from the legacy env flags for the two
    original C3 toggles so an existing NOTIFY_DOOR_OPEN=false keeps working
    until the user touches the UI."""
    base = ALERT_DEFAULTS.get(kind, {}).get("enabled", True)
    if kind in ("c3_open", "c3_unlocked"):
        from .config import get_settings

        s = get_settings()
        env = s.notify_door_open if kind == "c3_open" else s.notify_unlocked
        return base and bool(env)
    return base


def get_alert_config(kind: str) -> dict:
    base = ALERT_DEFAULTS.get(kind, {"enabled": True, "from": None, "to": None})
    en_raw = get_setting(f"alert.{kind}.enabled", None)
    enabled = _as_bool(en_raw) if en_raw is not None else _default_enabled(kind)
    return {
        "enabled": enabled,
        "from": _as_hour(get_setting(f"alert.{kind}.from", None), base.get("from")),
        "to": _as_hour(get_setting(f"alert.{kind}.to", None), base.get("to")),
    }


def set_alert_config(
    kind: str,
    *,
    enabled: bool | None = None,
    hour_from: Any = None,
    hour_to: Any = None,
) -> None:
    """Persist alert config. None = leave unchanged; "" = clear the window
    (fire any time); an int/string hour = set it."""
    if kind not in ALERT_DEFAULTS:
        raise ValueError(f"Unknown alert: {kind!r}")
    if enabled is not None:
        set_setting(f"alert.{kind}.enabled", "1" if enabled else "0")
    if hour_from is not None:
        set_setting(f"alert.{kind}.from", _hour_str(hour_from))
    if hour_to is not None:
        set_setting(f"alert.{kind}.to", _hour_str(hour_to))


def alert_enabled(kind: str) -> bool:
    return get_alert_config(kind)["enabled"]


def in_alert_window(kind: str, hour: int) -> bool:
    """True when `hour` (local) is inside the alert's quiet-hour window.
    No window (from/to None) → always true. Wraps midnight when from > to."""
    cfg = get_alert_config(kind)
    hf, ht = cfg["from"], cfg["to"]
    if hf is None or ht is None or hf == ht:
        return True
    if hf < ht:
        return hf <= hour < ht
    return hour >= hf or hour < ht  # wrap (e.g. 20 → 6)


def all_alert_configs() -> list[dict]:
    """Stable-ordered list of {kind, label, enabled, from, to} for the UI."""
    return [
        {"kind": k, "label": ALERT_LABELS.get(k, k), **get_alert_config(k)}
        for k in ALERT_DEFAULTS
    ]


# --- Automation configuration (per-rule enable + execution mode) --------------
# Rules live in automations.py; this is the generic persistence. Default enabled
# is False (opt-in — rules move real hardware) and default mode "vorschlag"
# (suggest + 1-tap), but each rule can override via the get() call.
AUTOMATION_MODES = ("autonom", "vorschlag")


def get_automation_config(
    rule_id: str,
    *,
    default_enabled: bool = False,
    default_mode: str = "vorschlag",
    default_after: int | None = None,
) -> dict:
    en_raw = get_setting(f"automation.{rule_id}.enabled", None)
    mode = get_setting(f"automation.{rule_id}.mode", None)
    return {
        "enabled": _as_bool(en_raw, default_enabled),
        "mode": mode if mode in AUTOMATION_MODES else default_mode,
        # Optional per-rule "act from this local hour" threshold (used by
        # time-triggered rules; None for rules that don't need it).
        "after": _as_hour(get_setting(f"automation.{rule_id}.after", None), default_after),
    }


def set_automation_config(
    rule_id: str,
    *,
    enabled: bool | None = None,
    mode: str | None = None,
    after: Any = None,
) -> None:
    if enabled is not None:
        set_setting(f"automation.{rule_id}.enabled", "1" if enabled else "0")
    if mode is not None:
        if mode not in AUTOMATION_MODES:
            raise ValueError(f"Unknown mode: {mode!r}")
        set_setting(f"automation.{rule_id}.mode", mode)
    if after is not None:
        set_setting(f"automation.{rule_id}.after", _hour_str(after))


def blended_price_ct(days: int = 90) -> tuple[float, str]:
    """Weighted-avg ct/kWh across complete charge sessions in the window.

    Each session contributes its own price (or the app default when unset),
    weighted by its energy. Falls back to the app default if there is no
    usable session data. Returns (price, source) where source is one of
    "blended", "default".
    """
    from datetime import timedelta

    from .db.models import ChargeSession

    default = get_default_price_ct()
    since = utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        stmt = (
            select(ChargeSession)
            .where(ChargeSession.is_complete.is_(True))
            .where(ChargeSession.started_at >= since)
        )
        rows = list(s.exec(stmt).all())
    weighted = 0.0
    energy = 0.0
    for r in rows:
        e = r.energy_kwh or 0
        if e <= 0:
            continue
        p = r.price_ct_per_kwh if r.price_ct_per_kwh is not None else default
        weighted += e * p
        energy += e
    if energy <= 0:
        return default, "default"
    return round(weighted / energy, 2), "blended"
