from __future__ import annotations

from datetime import datetime

from polestar_dash.journey_import import (
    geo_label_for,
    get_address_geofences,
    set_address_geofences,
    upsert,
)


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
    ins1, skip1 = upsert([rec], vin="YS3TEST0000000001")
    assert (ins1, skip1) == (1, 0)

    # Same trip, no VIN selected this time → must be recognised as a duplicate.
    ins2, skip2 = upsert([rec], vin=None)
    assert (ins2, skip2) == (0, 1)

    # And the reverse direction (no-VIN first, VIN second) also dedupes.
    ins3, _ = upsert([_rec(9)], vin=None)
    assert ins3 == 1
    ins4, skip4 = upsert([_rec(9)], vin="YS3TEST0000000001")
    assert (ins4, skip4) == (0, 1)


def test_geofence_label_radius(tmp_db):
    set_address_geofences({"Arbeit": {"lat": 46.64128, "lng": 14.29127, "radius_m": 150}})
    gf = get_address_geofences()
    assert gf["Arbeit"]["radius_m"] == 150.0

    # Eiskellerstraße 5 (~41 m) is inside; Kroneplatz 1 (~168 m) is outside.
    assert geo_label_for(46.6412, 14.29074, gf) == "Arbeit"
    assert geo_label_for(46.64161, 14.28913, gf) is None
    assert geo_label_for(None, None, gf) is None
