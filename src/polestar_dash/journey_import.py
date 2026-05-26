"""Parse and persist the Polestar app's journey-log CSV export.

The official Polestar app exports a CSV with columns:
    Start Date, End Date, Start Address, End Address,
    Distance in KM, Consumption in Kwh, Category,
    Start Latitude, Start Longitude, End Latitude, End Longitude,
    Start Odometer, End Odometer, Trip Type, SOC Source, SOC Destination,
    Comments

Dates are in the user's *local* time-zone, formatted "YYYY-MM-DD, HH:MM".
We accept a `tz_offset_hours` so callers can normalise to UTC for joining
with our snapshot-derived trips. Default 0 = treat as already-UTC.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta

from .util import utcnow

from sqlmodel import Session, select

from .db.models import JourneyImport
from .db.session import get_engine

_DATE_FMT = "%Y-%m-%d, %H:%M"

# Default address labels: one label may match multiple address substrings
# (e.g. "Lidl" could match several store addresses). Stored in the
# `address_labels` AppSetting as JSON of shape {label: [pattern, ...]}.
DEFAULT_ADDRESS_LABELS: dict[str, list[str]] = {}


def get_address_labels() -> dict[str, list[str]]:
    import json

    from .app_settings import get_setting

    raw = get_setting("address_labels", None)
    if not raw:
        return {k: list(v) for k, v in DEFAULT_ADDRESS_LABELS.items()}
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError
        out: dict[str, list[str]] = {}
        for label, patterns in parsed.items():
            if isinstance(patterns, list):
                out[str(label)] = [str(p) for p in patterns if p]
            elif isinstance(patterns, str):
                # tolerate the old flat {pattern: label} format on migration
                out.setdefault(str(patterns), []).append(str(label))
        return out or {k: list(v) for k, v in DEFAULT_ADDRESS_LABELS.items()}
    except (ValueError, TypeError):
        return {k: list(v) for k, v in DEFAULT_ADDRESS_LABELS.items()}


def set_address_labels(labels: dict[str, list[str]]) -> None:
    import json

    from .app_settings import set_setting

    clean: dict[str, list[str]] = {}
    for label, patterns in labels.items():
        lbl = str(label).strip()
        pats = [str(p).strip() for p in patterns if str(p).strip()]
        if lbl and pats:
            clean[lbl] = pats
    set_setting("address_labels", json.dumps(clean, ensure_ascii=False))


def label_for(
    address: str | None,
    labels: dict[str, list[str]] | None = None,
) -> str | None:
    """Return the friendly label if any pattern matches, else None."""
    if not address:
        return None
    table = labels if labels is not None else get_address_labels()
    addr_lower = address.lower()
    for label, patterns in table.items():
        for needle in patterns:
            if needle and needle.lower() in addr_lower:
                return label
    return None


def _to_float(s: str | None) -> float | None:
    if s is None or s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(s: str | None) -> int | None:
    v = _to_float(s)
    return int(v) if v is not None else None


def parse_csv(content: bytes, tz_offset_hours: float = 0.0) -> list[dict]:
    """Parse the journey-log CSV. Returns dicts ready for JourneyImport(**d)."""
    text = content.decode("utf-8-sig")  # strips BOM
    reader = csv.DictReader(io.StringIO(text))
    out: list[dict] = []
    offset = timedelta(hours=tz_offset_hours)
    for row in reader:
        start = row.get("Start Date") or ""
        end = row.get("End Date") or ""
        if not start or not end:
            continue
        try:
            started_at = datetime.strptime(start, _DATE_FMT) - offset
            ended_at = datetime.strptime(end, _DATE_FMT) - offset
        except ValueError:
            continue
        out.append({
            "started_at": started_at,
            "ended_at": ended_at,
            "start_address": row.get("Start Address") or None,
            "end_address": row.get("End Address") or None,
            "start_lat": _to_float(row.get("Start Latitude")),
            "start_lng": _to_float(row.get("Start Longitude")),
            "end_lat": _to_float(row.get("End Latitude")),
            "end_lng": _to_float(row.get("End Longitude")),
            "distance_km": _to_float(row.get("Distance in KM")),
            "consumption_kwh": _to_float(row.get("Consumption in Kwh")),
            "start_odometer_km": _to_float(row.get("Start Odometer")),
            "end_odometer_km": _to_float(row.get("End Odometer")),
            "soc_start": _to_int(row.get("SOC Source")),
            "soc_end": _to_int(row.get("SOC Destination")),
            "trip_type": (row.get("Trip Type") or None) or None,
            "category": (row.get("Category") or None) or None,
            "comments": (row.get("Comments") or None) or None,
        })
    return out


def upsert(records: list[dict], vin: str | None = None) -> tuple[int, int]:
    """Insert records, skipping duplicates on (started_at, ended_at, vin).

    Returns (inserted, skipped)."""
    inserted = 0
    skipped = 0
    with Session(get_engine()) as s:
        for rec in records:
            stmt = select(JourneyImport).where(
                JourneyImport.started_at == rec["started_at"],
                JourneyImport.ended_at == rec["ended_at"],
            )
            if vin is not None:
                stmt = stmt.where(JourneyImport.vin == vin)
            else:
                # Without an explicit IS NULL, SQL's three-valued logic lets
                # rows with vin=NULL slip past the equality filter, so a
                # re-import of the same CSV with no VIN selected would
                # accumulate duplicates.
                stmt = stmt.where(JourneyImport.vin.is_(None))
            if s.exec(stmt).first() is not None:
                skipped += 1
                continue
            s.add(JourneyImport(vin=vin, **rec))
            inserted += 1
        s.commit()
    return inserted, skipped


def tag_co2_for_imports() -> int:
    """Stamp `co2_g_per_kwh` on imported trips that don't have it yet.

    Uses the AT monthly grid-mix profile (or override). Returns how many
    rows were updated.
    """
    from .co2 import co2_g_per_kwh
    from .db.models import JourneyImport as J

    updated = 0
    with Session(get_engine()) as s:
        rows = list(s.exec(select(J).where(J.co2_g_per_kwh.is_(None))).all())
        for r in rows:
            r.co2_g_per_kwh = co2_g_per_kwh(r.started_at)
            s.add(r)
            updated += 1
        s.commit()
    return updated


_CALIB_WINDOW = timedelta(days=730)  # 2 years — keeps the calibration current


def fleet_avg_kwh_per_km() -> float:
    """Energy per km derived from imported trips, with a sensible default.

    Used as a fallback for short trips where the SoC didn't tick down by
    even 1 % — without this, the kWh column on those trips reads as `—`."""
    from .db.models import JourneyImport as J

    cutoff = utcnow() - _CALIB_WINDOW
    try:
        with Session(get_engine()) as s:
            rows = list(s.exec(select(J).where(J.started_at >= cutoff)).all())
        total_e = sum(
            (j.consumption_kwh or 0)
            for j in rows
            if j.consumption_kwh and j.distance_km and j.distance_km > 0
        )
        total_km = sum(
            (j.distance_km or 0)
            for j in rows
            if j.consumption_kwh and j.distance_km and j.distance_km > 0
        )
        if total_km > 0 and total_e > 0:
            return total_e / total_km
    except Exception:
        pass
    return 0.18  # ~18 kWh/100km Polestar-2 baseline


def calibrate_battery_kwh(min_soc_drop: int = 4) -> float | None:
    """Estimate usable battery capacity from imported trips.

    Battery kWh ≈ consumption_kwh / (soc_drop / 100). Restricted to trips
    with a meaningful SoC drop (≥ `min_soc_drop` %) so 1 % rounding noise
    doesn't dominate. Returns the energy-weighted mean kWh-per-percent ×
    100, or None if there's not enough data.
    """
    from .db.models import JourneyImport as J

    cutoff = utcnow() - _CALIB_WINDOW
    with Session(get_engine()) as s:
        rows = list(s.exec(select(J).where(J.started_at >= cutoff)).all())

    total_e = 0.0
    total_drop = 0
    for j in rows:
        e = j.consumption_kwh
        if e is None or e <= 0:
            continue
        if j.soc_start is None or j.soc_end is None:
            continue
        drop = j.soc_start - j.soc_end
        if drop < min_soc_drop:
            continue
        total_e += e
        total_drop += drop
    if total_drop <= 0:
        return None
    return round(total_e / total_drop * 100, 1)


def list_imports(
    days: int = 30,
    vin: str | None = None,
) -> list[JourneyImport]:
    since = utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        stmt = (
            select(JourneyImport)
            .where(JourneyImport.started_at >= since)
            .order_by(JourneyImport.started_at.desc())
        )
        if vin is not None:
            stmt = stmt.where(JourneyImport.vin == vin)
        return list(s.exec(stmt).all())
