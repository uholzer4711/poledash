"""Tests for idle-drain detection: drive rejection + C3 cause attribution."""

from __future__ import annotations

from datetime import UTC, timedelta

from sqlmodel import Session

from polestar_dash.db.models import C3Snapshot, VehicleSnapshot
from polestar_dash.db.session import get_engine
from polestar_dash.util import utcnow


def _snap(ts, soc, odo=100.0):
    return VehicleSnapshot(
        vin="VIN1", fetched_at=ts, soc_percent=soc, odometer_km=odo,
        charging_status="Idle",
    )


def _epoch(dt) -> int:
    return int(dt.replace(tzinfo=UTC).timestamp())


def _c3(ts, *, lat=None, lng=None, parked_ts=None,
        climate_active=False, air_running=False):
    # Real C3 rows always carry a parked/gps stamp; default it to a long-ago
    # park so tests exercise the "genuine deep park" path unless they override.
    if parked_ts is None and lat is not None:
        parked_ts = _epoch(ts - timedelta(hours=3))
    return C3Snapshot(
        vin="VIN1", fetched_at=ts, lat=lat, lng=lng, gps_ts=parked_ts,
        parked_ts=parked_ts, climate_active=climate_active, air_running=air_running,
    )


# A spot and one ~3 km away — far enough to read as "drove".
HOME = (46.6172, 14.5984)
AWAY = (46.6415, 14.6390)


def test_climate_and_conditioning_attribution(tmp_db):
    from polestar_dash.idle_drain import detect_idle_drains

    t0 = utcnow() - timedelta(days=1)
    with Session(get_engine()) as s:
        # Drain 1: 80→76 over 30 min, climate running, parked since 2 h before,
        # GPS pinned at HOME the whole time → "climate".
        s.add(_snap(t0, 80))
        s.add(_snap(t0 + timedelta(minutes=30), 76))
        s.add(_c3(t0, lat=HOME[0], lng=HOME[1], parked_ts=_epoch(t0 - timedelta(hours=2))))
        s.add(_c3(t0 + timedelta(minutes=15), lat=HOME[0], lng=HOME[1],
                  climate_active=True, parked_ts=_epoch(t0 - timedelta(hours=2))))
        s.add(_c3(t0 + timedelta(minutes=35), lat=HOME[0], lng=HOME[1],
                  parked_ts=_epoch(t0 - timedelta(hours=2))))
        # Drain 2 (separate, 8 h later): 76→73 over 45 min, climate idle, GPS
        # pinned, parked for hours → kW-level draw → "conditioning".
        t1 = t0 + timedelta(hours=8)
        s.add(_snap(t1, 76))
        s.add(_snap(t1 + timedelta(minutes=45), 73))
        s.add(_c3(t1, lat=HOME[0], lng=HOME[1]))
        s.add(_c3(t1 + timedelta(minutes=20), lat=HOME[0], lng=HOME[1]))
        s.add(_c3(t1 + timedelta(minutes=50), lat=HOME[0], lng=HOME[1]))
        s.commit()

    drains = detect_idle_drains(vin="VIN1", days=3)
    assert len(drains) == 2
    assert drains[0].cause == "climate"
    assert drains[0].parked_for_min == 120
    assert drains[1].cause == "conditioning"


def test_low_power_long_drain_is_vampire(tmp_db):
    from polestar_dash.idle_drain import detect_idle_drains

    t0 = utcnow() - timedelta(days=1)
    with Session(get_engine()) as s:
        # 2% over 5 h while pinned at HOME ⇒ ~0.3 kW ⇒ parasitic standby.
        s.add(_snap(t0, 60))
        s.add(_snap(t0 + timedelta(hours=5), 58))
        s.add(_c3(t0, lat=HOME[0], lng=HOME[1]))
        s.add(_c3(t0 + timedelta(hours=5, minutes=5), lat=HOME[0], lng=HOME[1]))
        s.commit()

    drains = detect_idle_drains(vin="VIN1", days=3)
    assert len(drains) == 1
    assert drains[0].cause == "vampire"
    assert drains[0].implied_kw < 1.0


def test_drive_with_frozen_odometer_is_rejected(tmp_db):
    """The June-3 bug: odometer frozen, SoC falling — but C3 GPS shows the car
    ended up somewhere else. Must NOT be reported as a parked drain."""
    from polestar_dash.idle_drain import detect_idle_drains

    t0 = utcnow() - timedelta(days=1)
    with Session(get_engine()) as s:
        # Odometer identical (frozen) across the whole window.
        s.add(_snap(t0, 70, odo=500.0))
        s.add(_snap(t0 + timedelta(minutes=30), 63, odo=500.0))
        # GPS frozen at the departure point, then the arrival fix appears within
        # ±pad of where the SoC stops falling (C3's mid-drive freeze).
        s.add(_c3(t0, lat=HOME[0], lng=HOME[1]))
        s.add(_c3(t0 + timedelta(minutes=20), lat=HOME[0], lng=HOME[1]))
        s.add(_c3(t0 + timedelta(minutes=38), lat=AWAY[0], lng=AWAY[1]))
        s.commit()

    drains = detect_idle_drains(vin="VIN1", days=3)
    assert drains == []


def test_drive_level_power_without_gps_is_rejected(tmp_db):
    """No C3 coverage at all: a parked car cannot draw drive-level power, so an
    implausible implied kW is rejected by the physics backstop."""
    from polestar_dash.idle_drain import detect_idle_drains

    t0 = utcnow() - timedelta(days=1)
    with Session(get_engine()) as s:
        # 7% over 30 min ⇒ ~11 kW ⇒ impossible for a standing car.
        s.add(_snap(t0, 70))
        s.add(_snap(t0 + timedelta(minutes=30), 63))
        s.commit()

    drains = detect_idle_drains(vin="VIN1", days=3)
    assert drains == []


def test_drop_right_after_parking_is_settling(tmp_db):
    """SoC relaxes for a while after a drive (OCV re-estimation). A modest drop
    that starts < SETTLE_MIN after the car parked is that settling, not draw."""
    from polestar_dash.idle_drain import detect_idle_drains

    t0 = utcnow() - timedelta(days=1)
    with Session(get_engine()) as s:
        # 2% over 25 min ⇒ ~3.9 kW (under the ceiling — so it's the *settling
        # guard*, not the power cap, that must reject this).
        s.add(_snap(t0, 72))
        s.add(_snap(t0 + timedelta(minutes=25), 70))
        parked = _epoch(t0 - timedelta(minutes=12))  # just arrived
        s.add(_c3(t0, lat=HOME[0], lng=HOME[1], parked_ts=parked))
        s.add(_c3(t0 + timedelta(minutes=28), lat=HOME[0], lng=HOME[1], parked_ts=parked))
        s.commit()

    drains = detect_idle_drains(vin="VIN1", days=3)
    assert drains == []


def test_drop_before_drive_with_stale_parkstamp_is_rejected(tmp_db):
    """The real June-11 bug: the car sat at work all morning (so its C3 park
    stamp is ~12 h old), the SoC fell 2% just before it drove home (odometer
    frozen at work, GPS frozen at the departure point), then the odometer jumped.
    The stale park stamp sails through the settling guard at a plausible ~3 kW —
    only the trailing odometer jump (`drove_after`) reveals it was a drive."""
    from polestar_dash.idle_drain import detect_idle_drains

    t0 = utcnow() - timedelta(days=1)
    stale_park = _epoch(t0 - timedelta(hours=12))
    with Session(get_engine()) as s:
        s.add(_snap(t0, 25, odo=2000.0))
        s.add(_snap(t0 + timedelta(minutes=30), 23, odo=2000.0))  # odo still frozen
        s.add(_snap(t0 + timedelta(minutes=50), 20, odo=2030.0))  # drove 30 km
        s.add(_c3(t0, lat=AWAY[0], lng=AWAY[1], parked_ts=stale_park))
        s.add(_c3(t0 + timedelta(minutes=35), lat=AWAY[0], lng=AWAY[1], parked_ts=stale_park))
        s.commit()

    drains = detect_idle_drains(vin="VIN1", days=3)
    assert drains == []


def test_standby_baseline_splits_trickle_from_predeparture_conditioning(tmp_db):
    """The June-2 shape: SoC holds flat for ~10 h while parked, then drops fast
    in the last half-hour before the car drives off. The flat trickle is
    standby; the pre-departure ramp is pre-conditioning — never standby."""
    from polestar_dash.idle_drain import standby_baseline

    t0 = utcnow() - timedelta(days=1)
    parked = _epoch(t0)
    with Session(get_engine()) as s:
        # 24 h parked at HOME (constant parked_ts ⇒ provably not moved; well
        # over MIN_STANDBY_HOURS): SoC holds 80 % the whole time, then 80→76 in
        # the final 30 min (≈8% → ~6 kW+, the conditioning ramp). 10-min cadence.
        steps = 24 * 6  # 144 polls over 24 h
        for k in range(steps + 1):
            ts = t0 + timedelta(minutes=10 * k)
            soc = 80 if k <= steps - 4 else 80 - (k - (steps - 4))  # last 4 steps drop
            s.add(_snap(ts, soc))
            s.add(_c3(ts, lat=HOME[0], lng=HOME[1], parked_ts=parked))
        s.commit()

    sb = standby_baseline(vin="VIN1", days=3)
    # Flat plateau dominates → standby rate is tiny; the ramp is pre-conditioning.
    assert sb["standby"] is not None
    assert sb["standby"]["pct_per_day"] < 0.5
    assert sb["preconditioning"] is not None
    assert sb["preconditioning"]["kwh"] > 1.0


def test_standby_baseline_ignores_drives(tmp_db):
    """A drive (parked_ts changes) must never feed the standby denominator."""
    from polestar_dash.idle_drain import standby_baseline

    t0 = utcnow() - timedelta(days=1)
    with Session(get_engine()) as s:
        # Two short parks with a drive between them, each < MIN_PARK_HOURS and
        # with distinct parked_ts → no qualifying parked time at all.
        s.add(_snap(t0, 60))
        s.add(_c3(t0, lat=HOME[0], lng=HOME[1], parked_ts=_epoch(t0)))
        s.add(_snap(t0 + timedelta(minutes=30), 50, odo=130.0))
        s.add(_c3(t0 + timedelta(minutes=30), lat=AWAY[0], lng=AWAY[1],
                  parked_ts=_epoch(t0 + timedelta(minutes=30))))
        s.commit()

    sb = standby_baseline(vin="VIN1", days=3)
    assert sb["standby"] is None  # not enough confirmed-parked time


def test_no_c3_modest_power_stays_unknown(tmp_db):
    from polestar_dash.idle_drain import detect_idle_drains

    t0 = utcnow() - timedelta(days=1)
    with Session(get_engine()) as s:
        # 3% over 90 min ⇒ ~1.6 kW ⇒ plausible parked draw, but no C3 to
        # attribute it → cause stays unknown (None) rather than guessing.
        s.add(_snap(t0, 60))
        s.add(_snap(t0 + timedelta(minutes=90), 57))
        s.commit()

    drains = detect_idle_drains(vin="VIN1", days=3)
    assert len(drains) == 1
    assert drains[0].cause is None
    assert drains[0].parked_for_min is None
