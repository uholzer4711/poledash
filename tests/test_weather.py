"""Tests for the Open-Meteo `current_at` cache + resilience layer."""

from __future__ import annotations

import httpx
import pytest

import polestar_dash.weather as weather


@pytest.fixture(autouse=True)
def _clear_cache():
    weather._current_cache.clear()
    yield
    weather._current_cache.clear()


@pytest.mark.asyncio
async def test_current_at_caches_and_dedups_fetches(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch(lat, lng):
        calls["n"] += 1
        return {"temp_c": 11.0, "code": 0, "precip_mm": 0, "is_rain": False, "bucket": "clear"}

    monkeypatch.setattr(weather, "_fetch_current", fake_fetch)

    a = await weather.current_at(46.6416, 14.2891)
    b = await weather.current_at(46.6418, 14.2893)  # same ~1 km cell
    assert a["temp_c"] == 11.0 and b == a
    assert calls["n"] == 1  # second call served from cache


@pytest.mark.asyncio
async def test_current_at_serves_stale_on_failure(monkeypatch):
    async def ok_fetch(lat, lng):
        return {"temp_c": 9.0, "code": 61, "precip_mm": 0.5, "is_rain": True, "bucket": "rain"}

    monkeypatch.setattr(weather, "_fetch_current", ok_fetch)
    first = await weather.current_at(46.0, 14.0)
    assert first["is_rain"] is True

    # Expire the TTL but stay within the stale window; next fetch fails.
    ts, val = weather._current_cache[(46.0, 14.0)]
    weather._current_cache[(46.0, 14.0)] = (ts - weather._CURRENT_TTL_S - 5, val)

    async def boom_fetch(lat, lng):
        raise httpx.ConnectTimeout("boom")

    monkeypatch.setattr(weather, "_fetch_current", boom_fetch)
    stale = await weather.current_at(46.0, 14.0)
    assert stale == first  # last good value, not None


@pytest.mark.asyncio
async def test_current_at_returns_none_without_usable_cache(monkeypatch):
    async def boom_fetch(lat, lng):
        raise httpx.ConnectTimeout("boom")

    monkeypatch.setattr(weather, "_fetch_current", boom_fetch)
    assert await weather.current_at(40.0, 10.0) is None


@pytest.mark.asyncio
async def test_fetch_current_retries_once(monkeypatch):
    attempts = {"n": 0}

    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"current": {"temperature_2m": 5.0, "weather_code": 3, "precipitation": 0}}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ReadTimeout("slow")
            return _Resp()

    monkeypatch.setattr(weather.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(weather.asyncio, "sleep", lambda *a, **k: _noop())

    out = await weather._fetch_current(46.0, 14.0)
    assert out["temp_c"] == 5.0 and out["bucket"] == "cloudy"
    assert attempts["n"] == 2  # failed once, succeeded on retry


async def _noop():
    return None
