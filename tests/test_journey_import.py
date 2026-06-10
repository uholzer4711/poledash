from __future__ import annotations

from datetime import datetime

import pytest
from sqlmodel import Session, select

from polestar_dash.journey_import import (
    geo_label_for,
    get_address_geofences,
    parse_csv,
    set_address_geofences,
    upsert,
)

_HDR = [
    "Start Date", "End Date", "Start Address", "End Address", "Distance in KM",
    "Consumption in Kwh", "Category", "Start Latitude", "Start Longitude",
    "End Latitude", "End Longitude", "Start Odometer", "End Odometer",
    "Trip Type", "SOC Source", "SOC Destination", "Comments",
]


def _row(start: str, end: str, km, kwh) -> list[str]:
    return [start, end, "A", "B", str(km), str(kwh), "Uncategorized",
            "46.5", "14.5", "46.6", "14.6", "100", "120", "SINGLE", "80", "75", ""]


def _file(rows: list[list[str]], delim: str = ",") -> bytes:
    # csv.writer quotes cells containing the delimiter — the dates
    # ("YYYY-MM-DD, HH:MM") contain commas, just like the real export.
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delim)
    w.writerow(_HDR)
    w.writerows(rows)
    return buf.getvalue().encode()


def test_parse_csv_kwh_passthrough():
    recs, meta = parse_csv(
        _file([_row("2026-06-01, 08:00", "2026-06-01, 08:30", 27, 4.253)])
    )
    assert meta["consumption_scaled"] is False
    assert recs[0]["consumption_kwh"] == 4.253
    assert recs[0]["consumption_scaled"] is False


def test_parse_csv_wh_tab_export_autoscales():
    """CarStatsViewer-style export: tab-separated, consumption in Wh."""
    rows = [_row("2026-06-01, 08:00", "2026-06-01, 08:30", 27, 4253)]
    recs, meta = parse_csv(_file(rows, delim="\t"))
    assert meta["consumption_scaled"] is True
    assert recs[0]["consumption_kwh"] == pytest.approx(4.253)
    assert recs[0]["consumption_scaled"] is True


def test_parse_csv_wh_file_keeps_decimal_kwh_cells():
    """Wh exports mix units per row: integer cells are Wh, decimal cells are
    already kWh (seen in real CarStatsViewer files) — only integers scale."""
    rows = [
        _row("2026-06-01, 08:00", "2026-06-01, 08:30", 27, 4253),  # Wh
        _row("2026-06-01, 09:00", "2026-06-01, 09:20", 13, 1.55),  # kWh leftover
    ]
    recs, meta = parse_csv(_file(rows, delim="\t"))
    assert meta["consumption_scaled"] is True
    assert recs[0]["consumption_kwh"] == pytest.approx(4.253)
    assert recs[0]["consumption_scaled"] is True
    assert recs[1]["consumption_kwh"] == pytest.approx(1.55)
    assert recs[1]["consumption_scaled"] is False


def test_parse_csv_inconclusive_unit_is_rejected():
    """All trips ≤ 0.5 km → no distance evidence → refuse to guess the unit."""
    rows = [_row("2026-06-01, 08:00", "2026-06-01, 08:05", 0, 36)]
    with pytest.raises(ValueError, match="kWh or Wh"):
        parse_csv(_file(rows))


def test_parse_csv_implausible_value_is_rejected():
    """A single-trip consumption above the whole pack means mixed/garbage data."""
    rows = [
        _row("2026-06-01, 08:00", "2026-06-01, 10:00", 200, 180),  # > pack size
        _row("2026-06-01, 11:00", "2026-06-01, 11:30", 30, 5.0),
    ]
    with pytest.raises(ValueError, match="implausible"):
        parse_csv(_file(rows))


def _rec(h: int = 8) -> dict:
    return {
        "started_at": datetime(2026, 5, 1, h, 0),
        "ended_at": datetime(2026, 5, 1, h, 20),
        "distance_km": 12.0,
        "consumption_kwh": 2.0,
    }


def test_upsert_dedup_is_vin_agnostic(tmp_db):
    """A re-import without a VIN must not duplicate rows first imported with one.

    Regression: dedup used to key on (started_at, ended_at, vin); mixing an
    import with a VIN and one without slipped past it and accumulated dupes.
    """
    rec = _rec()
    ins1, upd1, skip1 = upsert([rec], vin="YS3TEST0000000001")
    assert (ins1, upd1, skip1) == (1, 0, 0)

    # Same trip, no VIN selected this time → must be recognised as a duplicate.
    ins2, upd2, skip2 = upsert([rec], vin=None)
    assert (ins2, upd2, skip2) == (0, 0, 1)

    # And the reverse direction (no-VIN first, VIN second) also dedupes.
    ins3, _, _ = upsert([_rec(9)], vin=None)
    assert ins3 == 1
    ins4, upd4, skip4 = upsert([_rec(9)], vin="YS3TEST0000000001")
    assert (ins4, upd4, skip4) == (0, 0, 1)


def test_upsert_corrects_wrong_unit_duplicates(tmp_db):
    """Re-importing must repair rows stored with a wrong unit, not skip them."""
    from polestar_dash.db.models import JourneyImport
    from polestar_dash.db.session import get_engine

    raw = dict(_rec(), consumption_kwh=1912.0)  # stored raw-Wh pre-fix
    assert upsert([raw]) == (1, 0, 0)

    fixed = dict(_rec(), consumption_kwh=1.912, consumption_scaled=True)
    assert upsert([fixed]) == (0, 1, 0)
    with Session(get_engine()) as s:
        row = s.exec(select(JourneyImport)).one()
        assert row.consumption_kwh == pytest.approx(1.912)
        assert row.consumption_scaled is True

    # Unchanged re-import is a plain skip.
    assert upsert([fixed]) == (0, 0, 1)


def test_geofence_label_radius(tmp_db):
    set_address_geofences({"Arbeit": {"lat": 46.64128, "lng": 14.29127, "radius_m": 150}})
    gf = get_address_geofences()
    assert gf["Arbeit"]["radius_m"] == 150.0

    # Eiskellerstraße 5 (~41 m) is inside; Kroneplatz 1 (~168 m) is outside.
    assert geo_label_for(46.6412, 14.29074, gf) == "Arbeit"
    assert geo_label_for(46.64161, 14.28913, gf) is None
    assert geo_label_for(None, None, gf) is None
