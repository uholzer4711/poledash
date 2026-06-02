"""Tests for the C3 enrichment helpers, the rain/windows alert, and the
drive-cadence movement detection. Network (Open-Meteo) and notify() are stubbed
so nothing is actually fetched or sent."""

from __future__ import annotations

from datetime import timedelta

import pytest

from polestar_dash.db.models import C3Snapshot
from polestar_dash.util import haversine_km, utcnow


def _row(**kw) -> C3Snapshot:
    base = dict(vin="VIN1", fetched_at=utcnow(), lat=46.6, lng=14.3)
    base.update(kw)
    return C3Snapshot(**base)


def test_windows_open_lists_only_open():
    from polestar_dash.c3_anomalies import _windows_open

    row = _row(win_fl="open", win_fr="closed", sunroof="ajar", win_rl="—", win_rr=None)
    labels = set(_windows_open(row))
    assert "front-left window" in labels
    assert "sunroof" in labels
    assert "front-right window" not in labels
    assert len(labels) == 2


def test_wmo_rain_bucket():
    from polestar_dash.weather import RAIN_CODES, wmo_bucket

    assert wmo_bucket(0) == "clear"
    assert wmo_bucket(61) == "rain"
    assert wmo_bucket(95) == "thunderstorm"
    assert 61 in RAIN_CODES and 0 not in RAIN_CODES


@pytest.mark.asyncio
async def test_rain_alert_fires_only_on_rain_and_open(tmp_db, monkeypatch):
    import polestar_dash.c3_anomalies as ca

    sent: list[dict] = []
    monkeypatch.setattr(ca, "notify", lambda **kw: sent.append(kw))

    async def fake_rain(lat, lng):
        return {"is_rain": True, "bucket": "rain", "temp_c": 9, "code": 61}

    async def fake_dry(lat, lng):
        return {"is_rain": False, "bucket": "clear", "temp_c": 20, "code": 0}

    monkeypatch.setattr("polestar_dash.weather.current_at", fake_rain)

    # rain + a window open → one notification
    await ca.check_rain_windows(_row(win_fl="open"))
    assert len(sent) == 1
    assert sent[0]["kind"] == "c3_rain_windows"

    # rain but everything closed → no extra notification
    sent.clear()
    await ca.check_rain_windows(_row(win_fl="closed", sunroof="closed"))
    assert sent == []

    # window open but dry → no notification
    monkeypatch.setattr("polestar_dash.weather.current_at", fake_dry)
    await ca.check_rain_windows(_row(win_fl="open"))
    assert sent == []


def test_drive_detection_distance_threshold():
    # The c3_poll_once "moving" rule: > 0.3 km between consecutive fixes.
    near = haversine_km(46.6000, 14.3000, 46.6010, 14.3000)  # ~0.11 km
    far = haversine_km(46.6000, 14.3000, 46.6100, 14.3000)   # ~1.1 km
    assert near < 0.3
    assert far > 0.3


def test_nearest_c3_env_picks_window_values(tmp_db):
    from sqlmodel import Session

    from polestar_dash.c3_enrich import nearest_c3_env
    from polestar_dash.db.session import get_engine

    t0 = utcnow()
    with Session(get_engine()) as s:
        s.add(_row(fetched_at=t0, lat=46.61, lng=14.31, weather_temp_c=7.5, air_pm25=4.0))
        s.add(_row(fetched_at=t0 + timedelta(minutes=5), lat=46.62, lng=14.32))
        s.commit()

    env = nearest_c3_env(t0, t0 + timedelta(minutes=6))
    assert env is not None
    assert env["temp_c"] == 7.5
    assert env["pm25"] == 4.0
    assert env["lat"] is not None
