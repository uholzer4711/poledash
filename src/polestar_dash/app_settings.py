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
