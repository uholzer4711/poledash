"""Open-Meteo enrichment for imported journey-log trips.

For each JourneyImport row that has a start lat/lng but no `outside_temp_c`
yet, we look up the hourly temperature at the trip's start time. Open-Meteo
is free and key-less for hobbyist scale; we still batch to be polite.

API split:
- `/v1/forecast` covers ~14 days back + the future
- `/v1/archive` covers historical data (with a few-days delay)

We pick the right one based on age. Failures degrade silently — the trip
just stays un-enriched until the next run.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta

import httpx
from sqlmodel import Session, select

from .db.models import JourneyImport
from .db.session import get_engine

log = logging.getLogger(__name__)

_FORECAST_CUTOFF_DAYS = 6  # archive endpoint lags ~5 days behind real-time

# Open-Meteo's free endpoint regularly takes 5–9 s to answer and soft-throttles
# a chatty IP — so connect is cheap (TCP is instant) but the read is slow and
# right at any tight timeout's edge. Split the budget: short connect, generous
# read, and one retry, so a single slow response doesn't fail the lookup.
_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)

# "Current" weather only refreshes ~every 15 min upstream, yet we call this on
# every dashboard tick (60 s) + every C3 poll with a window open. A small TTL
# cache collapses that to roughly one real fetch per 10 min per location, which
# both keeps the dashboard instant and stops us hammering (and getting throttled
# by) Open-Meteo. On a fetch failure we serve the last good value if it's not
# yet ancient, so a transient timeout doesn't blank the widget / skip the rain
# alert. Keyed by coords rounded to ~1 km so GPS jitter doesn't bust the cache.
_CURRENT_TTL_S = 600.0       # serve cached without refetching
_CURRENT_STALE_MAX_S = 3600.0  # still serve (once) on failure up to this age
_current_cache: dict[tuple[float, float], tuple[float, dict]] = {}
_current_lock = asyncio.Lock()

# WMO weather codes that mean liquid precipitation (drizzle/rain/showers/storm).
# Snow (71–77, 85–86) is deliberately excluded — the alert is about rain getting
# into an open cabin. See https://open-meteo.com/en/docs (weather_code table).
RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}


def wmo_bucket(code: int | None) -> str:
    """Coarse English label for a WMO weather code (translated at the edge)."""
    if code is None:
        return "unknown"
    if code == 0:
        return "clear"
    if code in (1, 2, 3):
        return "cloudy"
    if code in (45, 48):
        return "fog"
    if code in (51, 53, 55, 56, 57):
        return "drizzle"
    if code in (61, 63, 65, 66, 67, 80, 81, 82):
        return "rain"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (95, 96, 99):
        return "thunderstorm"
    return "unknown"


async def _fetch_current(lat: float, lng: float) -> dict:
    """One real Open-Meteo 'current' request (no cache). Retries once on a
    network/timeout error; raises if both attempts fail."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lng:.4f}",
        "current": "temperature_2m,weather_code,precipitation",
        "timezone": "UTC",
    }
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.get(url, params=params)
                r.raise_for_status()
                cur = (r.json() or {}).get("current") or {}
            break
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
            if attempt == 0:
                await asyncio.sleep(1.0)
    else:
        raise last_exc  # both attempts were network failures

    code = cur.get("weather_code")
    precip = cur.get("precipitation")
    code_i = int(code) if code is not None else None
    return {
        "temp_c": cur.get("temperature_2m"),
        "code": code_i,
        "precip_mm": precip,
        "is_rain": (code_i in RAIN_CODES) or (isinstance(precip, (int, float)) and precip > 0.1),
        "bucket": wmo_bucket(code_i),
    }


async def current_at(lat: float, lng: float) -> dict | None:
    """Current weather at a coordinate via Open-Meteo (key-less), cached.

    Returns {temp_c, code, precip_mm, is_rain, bucket}, or None when there is no
    fetchable value at all. Within ``_CURRENT_TTL_S`` a cached value is returned
    without a network call; on a fresh fetch failure the last good value is
    served if it's younger than ``_CURRENT_STALE_MAX_S`` so a transient Open-Meteo
    timeout doesn't blank the dashboard or skip the rain alert."""
    key = (round(lat, 2), round(lng, 2))
    now = time.monotonic()

    cached = _current_cache.get(key)
    if cached is not None and (now - cached[0]) < _CURRENT_TTL_S:
        return cached[1]

    async with _current_lock:
        # Re-check: another awaiter may have refreshed while we waited.
        cached = _current_cache.get(key)
        now = time.monotonic()
        if cached is not None and (now - cached[0]) < _CURRENT_TTL_S:
            return cached[1]
        try:
            value = await _fetch_current(lat, lng)
        except Exception as e:  # noqa: BLE001
            # Expected, transient: log one line, no traceback for timeouts.
            if cached is not None and (now - cached[0]) < _CURRENT_STALE_MAX_S:
                log.warning(
                    "weather lookup failed (%s) at %.4f,%.4f — serving cached",
                    type(e).__name__, lat, lng,
                )
                return cached[1]
            log.warning(
                "weather lookup failed (%s) at %.4f,%.4f", type(e).__name__, lat, lng
            )
            return None
        _current_cache[key] = (time.monotonic(), value)
        return value


def _endpoint_for(d: date) -> str:
    age = (date.today() - d).days
    if age < _FORECAST_CUTOFF_DAYS:
        return "https://api.open-meteo.com/v1/forecast"
    return "https://archive-api.open-meteo.com/v1/archive"


async def _fetch_hourly(
    lat: float, lng: float, day: date, target: datetime
) -> tuple[float | None, int | None]:
    url = _endpoint_for(day)
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lng:.4f}",
        "start_date": day.isoformat(),
        "end_date": day.isoformat(),
        "hourly": "temperature_2m,weather_code",
        "timezone": "UTC",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    codes = hourly.get("weather_code") or []
    if not times:
        return None, None
    # Pick the hour closest to `target`.
    target_naive = target.replace(tzinfo=None)
    best_i = 0
    best_diff = timedelta.max
    for i, t in enumerate(times):
        try:
            ts = datetime.fromisoformat(t)
        except ValueError:
            continue
        diff = abs(ts - target_naive)
        if diff < best_diff:
            best_diff = diff
            best_i = i
    temp = temps[best_i] if best_i < len(temps) else None
    code = codes[best_i] if best_i < len(codes) else None
    return (float(temp) if temp is not None else None,
            int(code) if code is not None else None)


async def enrich_pending(limit: int = 50) -> dict:
    """Backfill `outside_temp_c` for imported trips that still lack it.

    Returns a small summary dict suitable for HTTP response."""
    enriched = 0
    skipped = 0
    failed = 0
    with Session(get_engine()) as s:
        stmt = (
            select(JourneyImport)
            .where(JourneyImport.outside_temp_c.is_(None))
            .where(JourneyImport.start_lat.is_not(None))
            .where(JourneyImport.start_lng.is_not(None))
            .order_by(JourneyImport.started_at.desc())
            .limit(limit)
        )
        rows = list(s.exec(stmt).all())

    for r in rows:
        try:
            temp, code = await _fetch_hourly(
                r.start_lat, r.start_lng, r.started_at.date(), r.started_at
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            # Transient network error — one line, no traceback. The trip stays
            # un-enriched and the next backfill run retries it.
            log.warning("weather lookup failed for trip %s (%s)", r.id, type(e).__name__)
            failed += 1
            continue
        except Exception:  # noqa: BLE001
            log.warning("weather lookup failed for trip %s", r.id, exc_info=True)
            failed += 1
            continue
        if temp is None:
            skipped += 1
            continue
        with Session(get_engine()) as s:
            row = s.get(JourneyImport, r.id)
            if row is None:
                continue
            row.outside_temp_c = temp
            row.weather_code = code
            s.add(row)
            s.commit()
        enriched += 1
    return {"enriched": enriched, "skipped": skipped, "failed": failed, "checked": len(rows)}
