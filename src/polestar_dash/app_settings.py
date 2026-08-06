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


def get_petrol_params() -> dict:
    """Petrol-car reference for the Insights ICE comparison. AppSetting
    overrides win over the config/env defaults."""
    from .config import get_settings

    s = get_settings()

    def _num(key: str, fallback: float) -> float:
        raw = get_setting(key, None)
        try:
            return float(raw) if raw is not None else float(fallback)
        except (TypeError, ValueError):
            return float(fallback)

    return {
        "price_eur_per_l": _num("petrol_price_eur_per_l", s.petrol_price_eur_per_l),
        "l_per_100km": _num("petrol_l_per_100km", s.petrol_l_per_100km),
        "co2_g_per_km": int(_num("petrol_co2_g_per_km", s.petrol_co2_g_per_km)),
    }


def set_petrol_params(
    price_eur_per_l: float | None = None,
    l_per_100km: float | None = None,
    co2_g_per_km: float | None = None,
) -> None:
    if price_eur_per_l is not None:
        if not 0 <= price_eur_per_l <= 10:
            raise ValueError("Petrol price must be 0–10 €/l")
        set_setting("petrol_price_eur_per_l", round(float(price_eur_per_l), 3))
    if l_per_100km is not None:
        if not 0 <= l_per_100km <= 30:
            raise ValueError("Consumption must be 0–30 l/100km")
        set_setting("petrol_l_per_100km", round(float(l_per_100km), 2))
    if co2_g_per_km is not None:
        if not 0 <= co2_g_per_km <= 500:
            raise ValueError("CO2 must be 0–500 g/km")
        set_setting("petrol_co2_g_per_km", int(co2_g_per_km))


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
    "c3_disconnected": {"enabled": True, "from": None, "to": None},
    "c3_climate": {"enabled": True, "from": None, "to": None},
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
    "c3_disconnected": "Vehicle connection lost / restored",
    "c3_climate": "Climate / preconditioning started / stopped",
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


# --- Spritmonitor.de export integration -------------------------------------
# Credentials + vehicle/tank mapping + auto-detected lookup IDs, all stored as
# plain AppSetting rows. Business logic (HTTP calls, field mapping) lives in
# spritmonitor.py; this is just persistence, mirroring the rest of this file.
SPRITMONITOR_BEARER_KEY = "spritmonitor_bearer_token"
SPRITMONITOR_APP_TOKEN_KEY = "spritmonitor_app_token"
# Public default Application-ID, same one the Home Assistant Spritmonitor
# integration (matbott/home_assistant_Spritmonitor) ships to all its users.
# Spritmonitor stopped answering individual app-token requests, so this is the
# de-facto community token; a user-supplied token in settings still overrides.
SPRITMONITOR_DEFAULT_APP_TOKEN = "095369dede84c55797c22d4854ca6efe"
SPRITMONITOR_VEHICLE_ID_KEY = "spritmonitor_vehicle_id"
SPRITMONITOR_VEHICLE_NAME_KEY = "spritmonitor_vehicle_name"
SPRITMONITOR_TANK_ID_KEY = "spritmonitor_tank_id"
SPRITMONITOR_AUTO_EXPORT_KEY = "spritmonitor_auto_export"
SPRITMONITOR_FUELSORT_ID_KEY = "spritmonitor_fuelsort_id"
SPRITMONITOR_QUANTITYUNIT_ID_KEY = "spritmonitor_quantityunit_id"
SPRITMONITOR_CURRENCY_ID_KEY = "spritmonitor_currency_id"
SPRITMONITOR_FIRST_DONE_KEY = "spritmonitor_first_export_done"


def get_spritmonitor_credentials() -> tuple[str, str] | None:
    bearer = get_setting(SPRITMONITOR_BEARER_KEY, None)
    if not bearer:
        return None
    app_token = get_setting(SPRITMONITOR_APP_TOKEN_KEY, None) or SPRITMONITOR_DEFAULT_APP_TOKEN
    return str(bearer), str(app_token)


def set_spritmonitor_credentials(bearer_token: str, app_token: str = "") -> None:
    if not bearer_token.strip():
        raise ValueError("Bearer token must not be empty")
    set_setting(SPRITMONITOR_BEARER_KEY, bearer_token.strip())
    # Empty app token = use SPRITMONITOR_DEFAULT_APP_TOKEN (stored empty so a
    # later change of the default takes effect without a resave).
    set_setting(SPRITMONITOR_APP_TOKEN_KEY, app_token.strip())


def get_spritmonitor_vehicle() -> dict | None:
    vid = get_setting(SPRITMONITOR_VEHICLE_ID_KEY, None)
    tid = get_setting(SPRITMONITOR_TANK_ID_KEY, None)
    if not vid or not tid:
        return None
    return {
        "vehicle_id": str(vid),
        "tank_id": str(tid),
        "vehicle_name": get_setting(SPRITMONITOR_VEHICLE_NAME_KEY, None),
    }


def set_spritmonitor_vehicle(
    vehicle_id: str, tank_id: str, vehicle_name: str | None = None
) -> None:
    set_setting(SPRITMONITOR_VEHICLE_ID_KEY, vehicle_id)
    set_setting(SPRITMONITOR_TANK_ID_KEY, tank_id)
    if vehicle_name:
        set_setting(SPRITMONITOR_VEHICLE_NAME_KEY, vehicle_name)


def get_spritmonitor_lookup_ids() -> dict | None:
    fs = get_setting(SPRITMONITOR_FUELSORT_ID_KEY, None)
    qu = get_setting(SPRITMONITOR_QUANTITYUNIT_ID_KEY, None)
    cur = get_setting(SPRITMONITOR_CURRENCY_ID_KEY, None)
    if fs is None or qu is None or cur is None:
        return None
    try:
        return {
            "fuelsort_id": int(fs),
            "quantityunit_id": int(qu),
            "currency_id": int(cur),
        }
    except (TypeError, ValueError):
        return None


def set_spritmonitor_lookup_ids(
    fuelsort_id: int, quantityunit_id: int, currency_id: int
) -> None:
    set_setting(SPRITMONITOR_FUELSORT_ID_KEY, int(fuelsort_id))
    set_setting(SPRITMONITOR_QUANTITYUNIT_ID_KEY, int(quantityunit_id))
    set_setting(SPRITMONITOR_CURRENCY_ID_KEY, int(currency_id))


def get_spritmonitor_auto_export() -> bool:
    return _as_bool(get_setting(SPRITMONITOR_AUTO_EXPORT_KEY, None), False)


def set_spritmonitor_auto_export(value: bool) -> None:
    set_setting(SPRITMONITOR_AUTO_EXPORT_KEY, "1" if value else "0")


def get_spritmonitor_first_export_done() -> bool:
    return _as_bool(get_setting(SPRITMONITOR_FIRST_DONE_KEY, None), False)


def set_spritmonitor_first_export_done() -> None:
    set_setting(SPRITMONITOR_FIRST_DONE_KEY, "1")


def spritmonitor_is_configured() -> bool:
    return (
        get_spritmonitor_credentials() is not None
        and get_spritmonitor_vehicle() is not None
    )


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


# --- Notification-channel / integration toggles ------------------------------
# Each channel is *configured* via env vars (config.py) and, on top of that,
# can be switched off from the UI without touching .env / restarting — e.g.
# temporarily muting WhatsApp while still keeping WAHA running. Default is on
# (unchanged behaviour) so an existing deployment doesn't go silent.
INTEGRATIONS = ("ntfy", "email", "telegram", "whatsapp", "mqtt")
INTEGRATION_LABELS = {
    "ntfy": "ntfy.sh push",
    "email": "Email",
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
    "mqtt": "MQTT / Home Assistant",
}


def integration_enabled(name: str) -> bool:
    return _as_bool(get_setting(f"integration.{name}.enabled", None), True)


def set_integration_enabled(name: str, enabled: bool) -> None:
    if name not in INTEGRATIONS:
        raise ValueError(f"Unknown integration: {name!r}")
    set_setting(f"integration.{name}.enabled", "1" if enabled else "0")


def all_integration_configs() -> list[dict]:
    """{id, label, configured, enabled} per channel, for the settings frame.
    `configured` reflects whether the env vars are set at all — the toggle
    only matters once a channel is configured."""
    from .config import get_settings

    s = get_settings()
    configured = {
        "ntfy": bool(s.ntfy_topic),
        "email": bool(s.smtp_host and s.smtp_to),
        "telegram": bool(s.telegram_bot_token and s.telegram_chat_id),
        "whatsapp": bool(s.whatsapp_url and s.whatsapp_api_key and s.whatsapp_to),
        "mqtt": bool(s.mqtt_host),
    }
    return [
        {
            "id": k,
            "label": INTEGRATION_LABELS[k],
            "configured": configured[k],
            "enabled": integration_enabled(k),
        }
        for k in INTEGRATIONS
    ]
