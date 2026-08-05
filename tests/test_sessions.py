from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

from polestar_dash.db.models import C3Snapshot, ChargeSession, VehicleSnapshot
from polestar_dash.db.session import get_engine
from polestar_dash.polestar.fetcher import Snapshot
from polestar_dash.sessions import _integrate_power_kwh, update_session


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


def _c3(ts, *, usage_mode=None, plugged=None):
    with Session(get_engine()) as s:
        s.add(C3Snapshot(vin="V", fetched_at=ts, usage_mode=usage_mode,
                         is_plugged_in=plugged))
        s.commit()


def test_c3_driving_vetoes_inferred_open(tmp_db):
    """Downhill recuperation looks like a silent charge to the main API (SoC
    up, odometer frozen mid-drive). A fresh C3 snapshot saying the car is
    driving / unplugged must veto the inferred open (real case: 2026-07-12,
    53→58% regen booked as a phantom 4.5 kWh session)."""
    t0 = datetime(2026, 7, 12, 12, 49, 0)
    _c3(t0 - timedelta(minutes=2), usage_mode="driving", plugged=False)
    update_session(_snap(t0, 56, 3868.1),
                   prev=_prev(t0 - timedelta(minutes=20), 53, 3868.1))
    assert _open() is None


def test_c3_unplugged_vetoes_inferred_open(tmp_db):
    """Even without usage_mode (older C3 rows), is_plugged_in=False alone
    rules out a silent charge."""
    t0 = datetime(2026, 7, 12, 12, 49, 0)
    _c3(t0 - timedelta(minutes=5), plugged=False)
    update_session(_snap(t0, 56, 3868.1),
                   prev=_prev(t0 - timedelta(minutes=20), 53, 3868.1))
    assert _open() is None


def test_c3_plugged_in_allows_inferred_open(tmp_db):
    """A genuine silent charge (plugged in, parked) must still open — the
    veto must not suppress the heuristic it was added to protect."""
    t0 = datetime(2026, 7, 10, 17, 30, 0)
    _c3(t0 - timedelta(minutes=3), usage_mode="inactive", plugged=True)
    update_session(_snap(t0, 44, 3719.8),
                   prev=_prev(t0 - timedelta(minutes=10), 41, 3719.8))
    cs = _open()
    assert cs is not None and cs.inferred is True


def test_stale_c3_does_not_veto(tmp_db):
    """A C3 row older than the freshness window (outage) must not veto —
    behaviour falls back to the pre-C3 heuristic."""
    t0 = datetime(2026, 7, 12, 12, 49, 0)
    _c3(t0 - timedelta(hours=3), usage_mode="driving", plugged=False)
    update_session(_snap(t0, 56, 3868.1),
                   prev=_prev(t0 - timedelta(minutes=20), 53, 3868.1))
    assert _open() is not None


def test_api_charging_ignores_c3_veto(tmp_db):
    """When the API itself says Charging, C3 state is irrelevant — the veto
    only guards the inference path."""
    t0 = datetime(2026, 7, 12, 12, 49, 0)
    _c3(t0 - timedelta(minutes=2), usage_mode="driving", plugged=False)
    update_session(_snap(t0, 56, 3868.1, status="Charging"),
                   prev=_prev(t0 - timedelta(minutes=20), 53, 3868.1))
    cs = _open()
    assert cs is not None and cs.inferred is False


def test_dc_fast_head_bridge_capped_tighter(tmp_db):
    """DC-fast sessions must not extend peak power across the full idle-poll
    gap the way AC sessions do — see 2026-07-23 session 18, where bridging
    93 kW over half a 10-min gap alone added ~7.75 kWh (23 %) to a 30-min DC
    charge. A high first-sample power (> DC_FAST_BRIDGE_POWER_W) caps the
    head bridge at DC_FAST_BRIDGE_CAP_S (2 min) instead of poll_interval_min."""
    with Session(get_engine()) as s:
        t0 = datetime(2026, 7, 23, 6, 9, 43)
        s.add(VehicleSnapshot(vin="V", fetched_at=t0 - timedelta(minutes=10),
                              soc_percent=59, odometer_km=100, charging_status="Idle"))
        s.add(VehicleSnapshot(vin="V", fetched_at=t0, soc_percent=60, odometer_km=100,
                              charging_status="Charging", charging_power_w=93000))
        s.add(VehicleSnapshot(vin="V", fetched_at=t0 + timedelta(minutes=2), soc_percent=67,
                              odometer_km=100, charging_status="Charging", charging_power_w=87000))
        s.commit()

        kwh = _integrate_power_kwh(s, "V", t0, t0 + timedelta(minutes=2))

    trapezoid_wh = (93000 + 87000) / 2 * (120 / 3600)
    capped_head_wh = 93000 * (120 / 2) / 3600  # DC_FAST_BRIDGE_CAP_S, not the 10-min gap
    assert kwh == pytest.approx((trapezoid_wh + capped_head_wh) / 1000, rel=1e-6)


def test_ac_head_bridge_uses_full_poll_interval(tmp_db):
    """Below the DC-fast threshold, the head bridge is unchanged: it still
    spans up to the full poll_interval_min gap (default 10 min)."""
    with Session(get_engine()) as s:
        t0 = datetime(2026, 7, 15, 17, 10, 23)
        s.add(VehicleSnapshot(vin="V", fetched_at=t0 - timedelta(minutes=10),
                              soc_percent=45, odometer_km=100, charging_status="Idle"))
        s.add(VehicleSnapshot(vin="V", fetched_at=t0, soc_percent=46, odometer_km=100,
                              charging_status="Charging", charging_power_w=11000))
        s.add(VehicleSnapshot(vin="V", fetched_at=t0 + timedelta(minutes=2), soc_percent=48,
                              odometer_km=100, charging_status="Charging", charging_power_w=11000))
        s.commit()

        kwh = _integrate_power_kwh(s, "V", t0, t0 + timedelta(minutes=2))

    trapezoid_wh = (11000 + 11000) / 2 * (120 / 3600)
    uncapped_head_wh = 11000 * (600 / 2) / 3600  # full 10-min poll_interval_min gap
    assert kwh == pytest.approx((trapezoid_wh + uncapped_head_wh) / 1000, rel=1e-6)


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
