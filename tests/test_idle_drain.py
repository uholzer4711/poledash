"""Tests for idle-drain detection + the C3-based cause attribution."""

from __future__ import annotations

from datetime import UTC, timedelta

from sqlmodel import Session

from polestar_dash.db.models import C3Snapshot, VehicleSnapshot
from polestar_dash.db.session import get_engine
from polestar_dash.util import utcnow


def _snap(ts, soc, odo=100.0):
    return VehicleSnapshot(
        vin="VIN1", fetched_at=ts, soc_percent=soc, odometer_km=odo,
        charging_status="Idle",
    )


def _epoch(dt) -> int:
    return int(dt.replace(tzinfo=UTC).timestamp())


def test_idle_drain_attribution(tmp_db):
    from polestar_dash.idle_drain import detect_idle_drains

    t0 = utcnow() - timedelta(days=1)
    with Session(get_engine()) as s:
        # Drain 1: SoC 80→76 while parked, climate running per C3 → "climate",
        # parked since 2 h before the drain started.
        s.add(_snap(t0, 80))
        s.add(_snap(t0 + timedelta(minutes=30), 76))
        s.add(C3Snapshot(
            vin="VIN1", fetched_at=t0 + timedelta(minutes=15),
            climate_active=True, parked_ts=_epoch(t0 - timedelta(hours=2)),
        ))
        # Drain 2 (6 h later, separate event): SoC 76→73, C3 saw the window
        # but nothing active → "standby".
        t1 = t0 + timedelta(hours=8)
        s.add(_snap(t1, 76))
        s.add(_snap(t1 + timedelta(minutes=45), 73))
        s.add(C3Snapshot(
            vin="VIN1", fetched_at=t1 + timedelta(minutes=20),
            climate_active=False, air_running=False,
        ))
        s.commit()

    drains = detect_idle_drains(vin="VIN1", days=3)
    assert len(drains) == 2
    assert drains[0].cause == "climate"
    assert drains[0].parked_for_min == 120
    assert drains[1].cause == "standby"
    assert drains[1].parked_for_min is None  # that row carries no parked fix


def test_idle_drain_without_c3_coverage_stays_unknown(tmp_db):
    from polestar_dash.idle_drain import detect_idle_drains

    t0 = utcnow() - timedelta(days=1)
    with Session(get_engine()) as s:
        s.add(_snap(t0, 60))
        s.add(_snap(t0 + timedelta(minutes=20), 57))
        s.commit()

    drains = detect_idle_drains(vin="VIN1", days=3)
    assert len(drains) == 1
    assert drains[0].cause is None
    assert drains[0].parked_for_min is None
