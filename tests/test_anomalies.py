"""The daily-dedup keys must be keyed on the *local* date. Europe/Vienna is
UTC+2 in summer, so a sample at 23:30 UTC happens at 01:30 the next local day —
a UTC-keyed dedup would roll over at the wrong wall-clock time (the v1.40.0 bug).
"""

from __future__ import annotations

from datetime import datetime

from polestar_dash import anomalies
from polestar_dash.db.models import VehicleSnapshot
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
