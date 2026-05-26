"""Grid CO2 intensity helper.

Austria's grid is ~80 % renewable annually but the mix swings seasonally
(more hydro in spring, more gas in winter). For session/trip tagging we
use a monthly profile rather than a flat constant. Numbers are rough
g CO2eq per kWh of delivered electricity (Austria 2024 averages, source:
e-control / electricitymap). Override via the `co2_g_per_kwh_override`
app setting or the CO2_INTENSITY env var for other regions.

This is not real-time. For hour-by-hour accuracy you'd plug in an
electricitymap API key, but the monthly profile is good enough to make
the per-session number feel honest.
"""

from __future__ import annotations

import os
from datetime import datetime

from .util import utcnow

_AT_MONTHLY_G_PER_KWH = {
    1: 220, 2: 200, 3: 150, 4: 120, 5: 100, 6: 90,
    7: 100, 8: 110, 9: 130, 10: 160, 11: 200, 12: 230,
}


def co2_g_per_kwh(when: datetime | None = None) -> int:
    raw = os.environ.get("CO2_INTENSITY")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    try:
        from .app_settings import get_setting

        override = get_setting("co2_g_per_kwh_override", None)
        if override:
            return int(float(override))
    except Exception:  # noqa: BLE001
        pass
    month = (when or utcnow()).month
    return _AT_MONTHLY_G_PER_KWH.get(month, 150)


def backfill_charge_sessions() -> int:
    """Stamp `co2_g_per_kwh` on existing complete charge sessions."""
    from sqlmodel import Session, select

    from .db.models import ChargeSession
    from .db.session import get_engine

    updated = 0
    with Session(get_engine()) as s:
        rows = list(s.exec(select(ChargeSession).where(ChargeSession.co2_g_per_kwh.is_(None))).all())
        for r in rows:
            r.co2_g_per_kwh = co2_g_per_kwh(r.started_at)
            s.add(r)
            updated += 1
        s.commit()
    return updated
