from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import Session, select

from polestar_dash.db.models import ChargeSession, VehicleSnapshot
from polestar_dash.db.session import get_engine
from polestar_dash.polestar.fetcher import Snapshot
from polestar_dash.sessions import update_session


def _snap(ts, soc, odo, status="Idle", **kw):
    return Snapshot(vin="V", fetched_at=ts, soc_percent=soc, odometer_km=odo,
                    charging_status=status, range_km=soc * 5, **kw)


def _prev(ts, soc, odo, status="Idle"):
    return VehicleSnapshot(vin="V", fetched_at=ts, soc_percent=soc, odometer_km=odo,
                           charging_status=status, range_km=soc * 5)


def _open(vin="V"):
    with Session(get_engine()) as s:
        return s.exec(
            select(ChargeSession).where(ChargeSession.vin == vin,
                                        ChargeSession.is_complete.is_(False))
        ).first()


def test_inferred_session_survives_soc_plateau(tmp_db):
    """A silent (inferred) charge must not close on a flat-SoC poll — the API
    status is 'Idle' the whole time, so an Idle plateau is not an end signal."""
    t0 = datetime(2026, 5, 28, 22, 0, 0)
    # Open inferred: +3% while parked, status Idle (API never says Charging).
    update_session(_snap(t0, 50, 100), prev=_prev(t0 - timedelta(minutes=10), 47, 100))
    cs = _open()
    assert cs is not None and cs.inferred is True

    # Plateau poll: SoC unchanged, still Idle, still parked → must stay open.
    closed = update_session(_snap(t0 + timedelta(minutes=10), 50, 100),
                            prev=_prev(t0, 50, 100))
    assert closed is None
    assert _open() is not None  # not closed

    # SoC rises again → continues.
    update_session(_snap(t0 + timedelta(minutes=20), 52, 100),
                   prev=_prev(t0 + timedelta(minutes=10), 50, 100))
    assert _open() is not None

    # Car driven away → now it closes, with energy computed.
    closed = update_session(_snap(t0 + timedelta(minutes=40), 52, 108),
                            prev=_prev(t0 + timedelta(minutes=20), 52, 100))
    assert closed is not None
    assert closed.is_complete is True
    assert closed.energy_kwh is not None and closed.energy_kwh > 0


def test_api_session_closes_on_idle(tmp_db):
    """A real API charge (status reported 'Charging') still closes the moment
    the status flips to a terminal 'Idle'/'Done'."""
    t0 = datetime(2026, 5, 28, 8, 0, 0)
    update_session(_snap(t0, 60, 200, status="Charging"),
                   prev=_prev(t0 - timedelta(minutes=2), 59, 200, status="Idle"))
    cs = _open()
    assert cs is not None and cs.inferred is False

    # Status flips to terminal Idle with SoC no longer rising → closes.
    closed = update_session(_snap(t0 + timedelta(minutes=2), 60, 200, status="Idle"),
                            prev=_prev(t0, 60, 200, status="Charging"))
    assert closed is not None and closed.is_complete is True
