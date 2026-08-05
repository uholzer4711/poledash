"""The daily-dedup keys must be keyed on the *local* date. Europe/Vienna is
UTC+2 in summer, so a sample at 23:30 UTC happens at 01:30 the next local day —
a UTC-keyed dedup would roll over at the wrong wall-clock time (the v1.40.0 bug).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import Session

from polestar_dash import anomalies
from polestar_dash.db.models import VehicleSnapshot
from polestar_dash.db.session import get_engine
from polestar_dash.polestar.fetcher import Snapshot


def _capture(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(anomalies, "notify", lambda **kw: calls.append(kw))
    return calls


def test_soc_drop_dedup_key_uses_local_date(tmp_db, monkeypatch):
    calls = _capture(monkeypatch)
    # 2026-06-18 23:30 UTC == 2026-06-19 01:30 CEST
    snap = Snapshot(vin="V", fetched_at=datetime(2026, 6, 18, 23, 30),
                    soc_percent=60, charging_status="Idle", odometer_km=100.0)
    prev = VehicleSnapshot(vin="V", fetched_at=datetime(2026, 6, 18, 23, 20),
                           soc_percent=72, charging_status="Idle", odometer_km=100.0)
    anomalies._check_soc_drop(snap, prev)
    assert len(calls) == 1
    key = calls[0]["key"]
    assert "2026-06-19" in key       # local day
    assert "2026-06-18" not in key   # not the UTC day


def test_power_collapse_dedup_key_uses_local_date(tmp_db, monkeypatch):
    calls = _capture(monkeypatch)
    snap = Snapshot(vin="V", fetched_at=datetime(2026, 6, 18, 23, 30),
                    soc_percent=50, charging_status="Charging",
                    charging_power_w=1000, target_soc_percent=90)
    prev = VehicleSnapshot(vin="V", fetched_at=datetime(2026, 6, 18, 23, 20),
                           soc_percent=49, charging_status="Charging",
                           charging_power_w=80_000)
    anomalies._check_power_collapse(snap, prev)
    assert len(calls) == 1
    assert "20260619" in calls[0]["key"]   # %Y%m%d, local


def _seed_range_history(vin: str, base: datetime, odometers: list[float]) -> None:
    """Five prior samples at a low (below-floor) km/% factor, 10 min apart,
    each with the given odometer reading — lets tests control whether the
    window looks stationary or actively driving."""
    with Session(get_engine()) as s:
        for i, odo in enumerate(odometers):
            s.add(VehicleSnapshot(
                vin=vin,
                fetched_at=base - timedelta(minutes=10 * (len(odometers) - i)),
                range_km=200.0, soc_percent=90, odometer_km=odo,
            ))
        s.commit()


def test_range_factor_fires_when_stationary(tmp_db, monkeypatch):
    calls = _capture(monkeypatch)
    base = datetime(2026, 8, 5, 12, 0)
    _seed_range_history("V", base, [1000.0] * 5)  # parked, odometer flat
    snap = Snapshot(vin="V", fetched_at=base, range_km=200.0, soc_percent=90,
                     odometer_km=1000.0)
    anomalies._check_range_factor(snap)
    assert len(calls) == 1


def test_range_factor_suppressed_while_driving(tmp_db, monkeypatch):
    """A mountain climb (or any active drive) legitimately tanks the
    range-per-%-SoC factor via real consumption — that's not cold or
    degradation, so don't alert on it."""
    calls = _capture(monkeypatch)
    base = datetime(2026, 8, 5, 12, 0)
    _seed_range_history("V", base, [1000.0, 1000.5, 1001.5, 1002.5, 1003.5])
    snap = Snapshot(vin="V", fetched_at=base, range_km=200.0, soc_percent=90,
                     odometer_km=1004.0)
    anomalies._check_range_factor(snap)
    assert len(calls) == 0
