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

from sqlmodel import Session, select

from .db.models import JourneyImport
from .db.session import get_engine
from .util import haversine_km, utcnow

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


# Geofence labels: a label defined by a centre point + radius instead of an
# address substring. More robust than string patterns (geocoding varies street
# by street) — e.g. "Arbeit" = everything within 150 m of the office. Stored in
# the `address_geofences` AppSetting as JSON {label: {lat, lng, radius_m}}.
def get_address_geofences() -> dict[str, dict]:
    import json

    from .app_settings import get_setting

    raw = get_setting("address_geofences", None)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError
        out: dict[str, dict] = {}
        for label, geo in parsed.items():
            if not isinstance(geo, dict):
                continue
            lat, lng, radius = geo.get("lat"), geo.get("lng"), geo.get("radius_m")
            if lat is None or lng is None or radius is None:
                continue
            out[str(label)] = {
                "lat": float(lat),
                "lng": float(lng),
                "radius_m": float(radius),
            }
        return out
    except (ValueError, TypeError):
        return {}


def set_address_geofences(geofences: dict[str, dict]) -> None:
    import json

    from .app_settings import set_setting

    clean: dict[str, dict] = {}
    for label, geo in geofences.items():
        lbl = str(label).strip()
        if not lbl or not isinstance(geo, dict):
            continue
        try:
            clean[lbl] = {
                "lat": float(geo["lat"]),
                "lng": float(geo["lng"]),
                "radius_m": float(geo["radius_m"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
    set_setting("address_geofences", json.dumps(clean, ensure_ascii=False))


def geo_label_for(
    lat: float | None,
    lng: float | None,
    geofences: dict[str, dict] | None = None,
) -> str | None:
    """Return the label whose geofence centre is within radius of (lat, lng)."""
    if lat is None or lng is None:
        return None
    table = geofences if geofences is not None else get_address_geofences()
    for label, geo in table.items():
        if haversine_km(lat, lng, geo["lat"], geo["lng"]) * 1000 <= geo["radius_m"]:
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


# Unit-detection guardrails. Real EV use is ~0.1–0.4 kWh/km, so an implied
# energy-per-km above _WH_RATIO_THRESHOLD (≈10× the worst real case) can only
# mean the column is Wh. Below _MIN_EVIDENCE_KM the ratio is one odd trip
# (heavy HVAC on a 600 m hop can legitimately exceed 2 kWh/km), not evidence —
# we refuse to guess. _MAX_TRIP_KWH: no single trip can consume more than the
# whole pack (~80 kWh); anything above it after normalisation means mixed or
# garbage data.
_WH_RATIO_THRESHOLD = 2.0
_MIN_EVIDENCE_KM = 5.0
_MAX_TRIP_KWH = 150.0


def _normalize_consumption_unit(rows: list[dict]) -> bool:
    """Detect & fix Wh-valued exports mislabeled as kWh, in place.

    The column header is always "Consumption in Kwh", but some journey exports
    put **Wh** integers there (e.g. ``4253`` for a 27 km trip) instead of kWh
    decimals (``4.253``). Storing Wh into ``consumption_kwh`` is a silent
    1000× error that wrecks battery calibration, CO₂, cost and Wh/km insights.

    Detection is file-level over trips with usable distance, but scaling is
    per-row: Wh-valued exports mix units within one file — integer cells are
    Wh (``4253``) while decimal cells are already kWh (``1.55`` for a 13 km
    trip, cross-checked against the official export of the same trips). So in
    a Wh-verdict file only integral values are divided; decimals pass through.
    Each row is stamped with ``consumption_scaled`` so the DB keeps provenance
    of the conversion. Returns True if the file needed converting.

    Raises ValueError instead of guessing when the file has consumption values
    but not enough distance evidence, or when a value stays implausible after
    normalisation — rejecting the import beats silently storing wrong energies.
    """
    total_e = 0.0
    total_km = 0.0
    has_energy = False
    for r in rows:
        e, d = r.get("consumption_kwh"), r.get("distance_km")
        if e:
            has_energy = True
        if e is not None and d and d > 0.5:
            total_e += e
            total_km += d
    scaled = False
    if has_energy:
        if total_km < _MIN_EVIDENCE_KM:
            raise ValueError(
                "cannot tell whether consumption is in kWh or Wh: the file has "
                f"less than {_MIN_EVIDENCE_KM:g} km of trips with both distance "
                "and consumption — import aborted instead of guessing"
            )
        scaled = (total_e / total_km) > _WH_RATIO_THRESHOLD
    for r in rows:
        e = r.get("consumption_kwh")
        if e is None:
            r["consumption_scaled"] = None
            continue
        row_scaled = scaled and float(e).is_integer()
        if row_scaled:
            e = e / 1000.0
            r["consumption_kwh"] = e
        if e > _MAX_TRIP_KWH:
            raise ValueError(
                f"implausible consumption of {e:g} kWh on a single trip — "
                "mixed kWh/Wh data? import aborted"
            )
        r["consumption_scaled"] = row_scaled
    return scaled


def parse_csv(content: bytes, tz_offset_hours: float = 0.0) -> tuple[list[dict], dict]:
    """Parse the journey-log CSV into (records, meta).

    Records are dicts ready for JourneyImport(**d); meta["consumption_scaled"]
    reports whether the consumption column was auto-converted from Wh to kWh.
    Raises ValueError when the consumption unit cannot be determined safely
    (see _normalize_consumption_unit)."""
    text = content.decode("utf-8-sig")  # strips BOM
    # Delimiter auto-detect: the official Polestar app exports comma-separated,
    # but the CarStatsViewer-style export is tab-separated. Decide from the
    # header line only — addresses contain commas, so a body-wide comma count
    # would mislead, but the column names never do.
    header = text.partition("\n")[0]
    delimiter = "\t" if header.count("\t") > header.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
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
    scaled = _normalize_consumption_unit(out)
    return out, {"consumption_scaled": scaled}


def upsert(records: list[dict], vin: str | None = None) -> tuple[int, int, int]:
    """Insert records, deduping on (started_at, ended_at).

    Dedup is intentionally VIN-agnostic: a trip is uniquely identified by its
    exact start+end timestamps for this single-car app. Keying on VIN too was a
    footgun — importing the same CSV once *with* a VIN and once *without* (SQL's
    three-valued logic never matches ``'VIN' = NULL``) accumulated duplicates.
    ``vin`` is still stored as informational metadata.

    A duplicate whose consumption_kwh differs is *corrected* rather than
    skipped: re-importing a file must be able to repair rows that were stored
    with a wrong unit before the Wh→kWh auto-detection existed — otherwise a
    bad import would be permanent.

    Returns (inserted, updated, skipped)."""
    inserted = 0
    updated = 0
    skipped = 0
    with Session(get_engine()) as s:
        for rec in records:
            stmt = select(JourneyImport).where(
                JourneyImport.started_at == rec["started_at"],
                JourneyImport.ended_at == rec["ended_at"],
            )
            existing = s.exec(stmt).first()
            if existing is not None:
                new_e = rec.get("consumption_kwh")
                if new_e is not None and (
                    existing.consumption_kwh is None
                    or abs(existing.consumption_kwh - new_e) > 1e-6
                ):
                    existing.consumption_kwh = new_e
                    existing.consumption_scaled = rec.get("consumption_scaled")
                    s.add(existing)
                    updated += 1
                else:
                    skipped += 1
                continue
            s.add(JourneyImport(vin=vin, **rec))
            inserted += 1
        s.commit()
    return inserted, updated, skipped


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
