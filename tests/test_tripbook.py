from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import Session

from polestar_dash.db.models import VehicleSnapshot
from polestar_dash.db.session import get_engine
from polestar_dash.tripbook import detect_trips


def _insert(rows: list[dict]) -> None:
    with Session(get_engine()) as s:
        for r in rows:
            s.add(VehicleSnapshot(vin="V", **r))
        s.commit()


def test_event_ts_advance_recovers_full_drive_duration(tmp_db):
    """A drive whose odometer value ticks only once must still span the whole
    window the event timestamps describe — not collapse to the gap between
    consecutive odometer-value samples.

    Mirrors a real Polestar trace: the odometer reading lags behind motion and
    updates in a single batch, while `odometer_event_ts` advances each poll."""
    base = datetime(2026, 5, 28, 4, 0, 0)
    rows = [
        # parked
        {"fetched_at": base, "odometer_km": 1074.9,
         "odometer_event_ts": base - timedelta(hours=13), "soc_percent": 58, "charging_status": "Idle"},
        # event ts advances while odo value is still flat -> motion started
        {"fetched_at": base + timedelta(minutes=10), "odometer_km": 1074.9,
         "odometer_event_ts": base + timedelta(minutes=2, seconds=34), "soc_percent": 56, "charging_status": "Idle"},
        # odo value catches up
        {"fetched_at": base + timedelta(minutes=20), "odometer_km": 1082.7,
         "odometer_event_ts": base + timedelta(minutes=13, seconds=9), "soc_percent": 56, "charging_status": "Idle"},
        # event ts still advancing, odo value plateaued
        {"fetched_at": base + timedelta(minutes=30), "odometer_km": 1082.7,
         "odometer_event_ts": base + timedelta(minutes=22, seconds=56), "soc_percent": 55, "charging_status": "Idle"},
        # parked again (event ts frozen)
        {"fetched_at": base + timedelta(minutes=40), "odometer_km": 1082.7,
         "odometer_event_ts": base + timedelta(minutes=22, seconds=56), "soc_percent": 52, "charging_status": "Idle"},
        {"fetched_at": base + timedelta(minutes=50), "odometer_km": 1082.7,
         "odometer_event_ts": base + timedelta(minutes=22, seconds=56), "soc_percent": 52, "charging_status": "Idle"},
    ]
    _insert(rows)

    trips = detect_trips(vin="V", days=1)
    assert len(trips) == 1
    t = trips[0]
    assert t.km == 7.8
    # full event-ts span ~20 min, not the 3-min odometer-value gap
    assert t.duration_min >= 18
    assert t.started_at == base + timedelta(minutes=2, seconds=34)
    assert t.ended_at == base + timedelta(minutes=22, seconds=56)


def test_value_move_trip_anchors_start_to_parked_poll(tmp_db):
    """When the odometer value jumps across one poll gap with no prior
    event-ts pre-signal, the start anchors to the last parked poll."""
    base = datetime(2026, 5, 28, 10, 0, 0)
    rows = [
        {"fetched_at": base, "odometer_km": 100.0,
         "odometer_event_ts": base - timedelta(hours=2), "soc_percent": 80, "charging_status": "Idle"},
        {"fetched_at": base + timedelta(minutes=10), "odometer_km": 110.0,
         "odometer_event_ts": base + timedelta(minutes=8), "soc_percent": 77, "charging_status": "Idle"},
        {"fetched_at": base + timedelta(minutes=20), "odometer_km": 110.0,
         "odometer_event_ts": base + timedelta(minutes=8), "soc_percent": 77, "charging_status": "Idle"},
        {"fetched_at": base + timedelta(minutes=30), "odometer_km": 110.0,
         "odometer_event_ts": base + timedelta(minutes=8), "soc_percent": 77, "charging_status": "Idle"},
    ]
    _insert(rows)

    trips = detect_trips(vin="V", days=1)
    assert len(trips) == 1
    assert trips[0].start_km == 100.0
    assert trips[0].end_km == 110.0
    assert trips[0].started_at == base
