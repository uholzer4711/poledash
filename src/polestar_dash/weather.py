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
