from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session

from polestar_dash.db.models import VehicleSnapshot
from polestar_dash.db.session import get_engine
from polestar_dash.tripbook import _battery_kwh, detect_trips
from polestar_dash.util import utcnow


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
    base = utcnow().replace(microsecond=0) - timedelta(hours=2)
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
    base = utcnow().replace(microsecond=0) - timedelta(hours=2)
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


def test_short_trip_energy_uses_car_consumption_not_soc(tmp_db):
    """On a short trip the 1%-quantized SoC drop wildly overstates energy.
    When the car reports its own consumption figure, use that instead."""
    base = utcnow().replace(microsecond=0) - timedelta(hours=2)
    rows = [
        {"fetched_at": base, "odometer_km": 100.0,
         "odometer_event_ts": base - timedelta(hours=2), "soc_percent": 58,
         "avg_consumption_kwh_per_100km": 14.8, "charging_status": "Idle"},
        {"fetched_at": base + timedelta(minutes=10), "odometer_km": 107.8,
         "odometer_event_ts": base + timedelta(minutes=8), "soc_percent": 55,
         "avg_consumption_kwh_per_100km": 14.8, "charging_status": "Idle"},
        {"fetched_at": base + timedelta(minutes=20), "odometer_km": 107.8,
         "odometer_event_ts": base + timedelta(minutes=8), "soc_percent": 55,
         "avg_consumption_kwh_per_100km": 14.8, "charging_status": "Idle"},
        {"fetched_at": base + timedelta(minutes=30), "odometer_km": 107.8,
         "odometer_event_ts": base + timedelta(minutes=8), "soc_percent": 55,
         "avg_consumption_kwh_per_100km": 14.8, "charging_status": "Idle"},
    ]
    _insert(rows)

    t = detect_trips(vin="V", days=1)[0]
    assert t.km == 7.8
    # car efficiency: 14.8 kWh/100km, not the ~3%*83kWh/7.8km SoC estimate
    assert t.kwh_used == round(14.8 * 7.8 / 100, 2)
    assert abs(t.kwh_per_100km - 14.8) <= 0.2  # rounding through int wh/km


def test_short_trip_no_kwh_without_consumption(tmp_db):
    """A short trip with no car-reported consumption must yield kwh_used=None.
    The old SoC-drop fallback read as ~1300 Wh/km on a sub-km / 1% trip and
    poisoned monthly totals / insights — better no number than a wrong one."""
    base = utcnow().replace(microsecond=0) - timedelta(hours=2)
    rows = [
        {"fetched_at": base, "odometer_km": 100.0,
         "odometer_event_ts": base - timedelta(hours=2), "soc_percent": 58,
         "charging_status": "Idle"},
        {"fetched_at": base + timedelta(minutes=10), "odometer_km": 107.8,
         "odometer_event_ts": base + timedelta(minutes=8), "soc_percent": 55,
         "charging_status": "Idle"},
        {"fetched_at": base + timedelta(minutes=20), "odometer_km": 107.8,
         "odometer_event_ts": base + timedelta(minutes=8), "soc_percent": 55,
         "charging_status": "Idle"},
        {"fetched_at": base + timedelta(minutes=30), "odometer_km": 107.8,
         "odometer_event_ts": base + timedelta(minutes=8), "soc_percent": 55,
         "charging_status": "Idle"},
    ]
    _insert(rows)

    t = detect_trips(vin="V", days=1)[0]
    assert t.km == 7.8                              # below the 10 km SoC floor
    assert t.avg_consumption_kwh_per_100km is None
    assert t.kwh_used is None                       # no last-resort SoC estimate
    assert t.wh_per_km is None


def test_long_trip_prefers_soc_over_flat_avg(tmp_db):
    """On a long-enough trip the SoC drop is the honest per-trip energy. The
    car's avg-consumption is a slow lifetime average; using it would reprint the
    same efficiency for every trip and hide real variation."""
    base = utcnow().replace(microsecond=0) - timedelta(hours=2)
    rows = [
        {"fetched_at": base, "odometer_km": 100.0,
         "odometer_event_ts": base - timedelta(hours=2), "soc_percent": 81,
         "avg_consumption_kwh_per_100km": 14.8, "charging_status": "Idle"},
        {"fetched_at": base + timedelta(minutes=10), "odometer_km": 140.0,
         "odometer_event_ts": base + timedelta(minutes=8), "soc_percent": 72,
         "avg_consumption_kwh_per_100km": 14.8, "charging_status": "Idle"},
        {"fetched_at": base + timedelta(minutes=20), "odometer_km": 140.0,
         "odometer_event_ts": base + timedelta(minutes=8), "soc_percent": 72,
         "avg_consumption_kwh_per_100km": 14.8, "charging_status": "Idle"},
        {"fetched_at": base + timedelta(minutes=30), "odometer_km": 140.0,
         "odometer_event_ts": base + timedelta(minutes=8), "soc_percent": 72,
         "avg_consumption_kwh_per_100km": 14.8, "charging_status": "Idle"},
    ]
    _insert(rows)

    t = detect_trips(vin="V", days=1)[0]
    assert t.km == 40.0
    # 9% drop of a real battery, NOT the flat 14.8*40/100 = 5.92 kWh
    soc_kwh = round(9 / 100.0 * _battery_kwh(), 2)
    assert t.kwh_used == soc_kwh
    assert t.kwh_used > 14.8 * 40 / 100  # measurably above the flat-avg figure


def test_kwh_falls_back_to_soc_when_no_consumption(tmp_db):
    """With no car-reported consumption, fall back to the SoC-delta estimate."""
    base = utcnow().replace(microsecond=0) - timedelta(hours=2)
    rows = [
        {"fetched_at": base, "odometer_km": 200.0,
         "odometer_event_ts": base - timedelta(hours=2), "soc_percent": 80, "charging_status": "Idle"},
        {"fetched_at": base + timedelta(minutes=10), "odometer_km": 260.0,
         "odometer_event_ts": base + timedelta(minutes=8), "soc_percent": 70, "charging_status": "Idle"},
        {"fetched_at": base + timedelta(minutes=20), "odometer_km": 260.0,
         "odometer_event_ts": base + timedelta(minutes=8), "soc_percent": 70, "charging_status": "Idle"},
        {"fetched_at": base + timedelta(minutes=30), "odometer_km": 260.0,
         "odometer_event_ts": base + timedelta(minutes=8), "soc_percent": 70, "charging_status": "Idle"},
    ]
    _insert(rows)

    t = detect_trips(vin="V", days=1)[0]
    assert t.kwh_used is not None and t.kwh_used > 0
    assert t.avg_consumption_kwh_per_100km is None
