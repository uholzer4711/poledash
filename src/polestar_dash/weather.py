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

import logging
from datetime import date, datetime, timedelta

import httpx
from sqlmodel import Session, select

from .db.models import JourneyImport
from .db.session import get_engine

log = logging.getLogger(__name__)

_FORECAST_CUTOFF_DAYS = 6  # archive endpoint lags ~5 days behind real-time

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


async def current_at(lat: float, lng: float) -> dict | None:
    """Current weather at a coordinate via Open-Meteo (key-less).

    Returns {temp_c, code, precip_mm, is_rain, bucket} or None on failure."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lng:.4f}",
        "current": "temperature_2m,weather_code,precipitation",
        "timezone": "UTC",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            cur = (r.json() or {}).get("current") or {}
    except Exception:  # noqa: BLE001
        log.warning("current weather lookup failed at %s,%s", lat, lng, exc_info=True)
        return None
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
    async with httpx.AsyncClient(timeout=10.0) as client:
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
