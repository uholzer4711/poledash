"""Detect *non-trip* battery drain (pre-conditioning / climate / parasitic).

Heuristic core: between consecutive snapshots the car was not charging and the
SoC dropped while the car stayed put. That energy went somewhere — cabin
pre-conditioning, battery/thermal management, in-car services, or parasitic
standby.

The hard part is telling a genuine parked drain apart from an ordinary *drive*,
because two telemetry quirks conspire to disguise a drive as "parked":

  * The pypolestar **odometer freezes** — its value (and `odometer_event_ts`)
    can stay unchanged for *hours*, only jumping when the car next reports. So
    "odometer didn't change between two polls" does NOT mean the car stood
    still; it usually just means the odometer hasn't refreshed yet.
  * The C3 **GPS fix freezes at the departure point** for the whole drive and
    only updates to the arrival coordinates once parked. So a drive looks like
    "same position" until it suddenly teleports on arrival.

We therefore decide movement from **C3 GPS displacement** across the window
*plus a look-ahead* (to catch the delayed arrival fix), and back that up with a
**physical power ceiling**: a parked car cannot sustain drive-level power, so an
implied draw above ~8 kW with no GPS confirmation of standstill is treated as a
missed drive, not a drain. Confirmed-parked drains are then classified by their
implied power (cabin climate / active conditioning / parasitic).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from .app_settings import get_calibrated_battery_kwh
from .db.models import C3Snapshot, VehicleSnapshot
from .db.session import get_engine
from .util import haversine_km, utcnow

GAP_MINUTES = 360  # 6 h — max gap that still collapses into one drain event
MOVE_KM = 0.5  # GPS/odometer displacement that means the car drove, not parked
DRIVE_KW = 8.0  # implied power above this, with no proof of standstill, = a drive
CONDITIONING_KW = 2.0  # parked draw above this = active (battery/thermal) management
SETTLE_MIN = 30  # a drop within this long of parking is post-drive SoC settling /
#                  a mid-drive GPS freeze — not a real parked drain

# Valid `cause` values (drives are *excluded* from the result, never labelled):
#   "climate"      — C3 saw cabin pre-conditioning running
#   "air"          — C3 saw cabin air-cleaning running
#   "conditioning" — parked & climate idle, but kW-level draw (battery/thermal
#                    management or an in-car service), NOT tiny parasitic loss
#   "vampire"      — parked, low-power parasitic standby loss
#   None           — no C3 coverage to attribute (e.g. pre-C3 history)


@dataclass
class IdleDrain:
    started_at: datetime
    ended_at: datetime
    soc_start: int
    soc_end: int
    duration_min: int
    cause: str | None = None
    # Minutes the car had already been parked when the drain started (from the
    # C3 parked-fix timestamp) — parasitic loss shows up hours into a park,
    # conditioning right around departure/arrival.
    parked_for_min: int | None = None

    @property
    def soc_drop(self) -> int:
        return self.soc_start - self.soc_end

    @property
    def kwh(self) -> float:
        return round(self.soc_drop / 100.0 * get_calibrated_battery_kwh(), 2)

    @property
    def implied_kw(self) -> float:
        """Average power the SoC drop implies over the event. The headline
        plausibility number — a parked car physically cannot sustain
        drive-level kW, so this is what exposes a misclassified drive."""
        hours = self.duration_min / 60.0
        if hours <= 0:
            return 0.0
        return round(self.kwh / hours, 1)


def detect_idle_drains(vin: str | None = None, days: int = 30) -> list[IdleDrain]:
    since = utcnow() - timedelta(days=days)
    with Session(get_engine()) as s:
        stmt = (
            select(VehicleSnapshot)
            .where(VehicleSnapshot.fetched_at >= since)
            .where(VehicleSnapshot.soc_percent.is_not(None))
            .order_by(VehicleSnapshot.fetched_at.asc())
        )
        if vin:
            stmt = stmt.where(VehicleSnapshot.vin == vin)
        rows = list(s.exec(stmt).all())

    out: list[IdleDrain] = []
    cur: IdleDrain | None = None
    prev: VehicleSnapshot | None = None

    for r in rows:
        if prev is None:
            prev = r
            continue

        gap_min = (r.fetched_at - prev.fetched_at).total_seconds() / 60
        is_charging = r.charging_status in ("Charging", "Smart Charging")
        # Only a *positive* odometer advance is reliable evidence of driving
        # (the odometer only ever goes up). A frozen/equal reading proves
        # nothing — that is resolved later from C3 GPS in _classify().
        odo_advanced = (
            prev.odometer_km is not None
            and r.odometer_km is not None
            and (r.odometer_km - prev.odometer_km) >= MOVE_KM
        )
        soc_dropped = (
            prev.soc_percent is not None
            and r.soc_percent is not None
            and r.soc_percent < prev.soc_percent
        )

        if soc_dropped and not odo_advanced and not is_charging and gap_min <= GAP_MINUTES:
            if cur is None:
                cur = IdleDrain(
                    started_at=prev.fetched_at,
                    ended_at=r.fetched_at,
                    soc_start=prev.soc_percent,
                    soc_end=r.soc_percent,
                    duration_min=int(gap_min),
                )
            else:
                cur.ended_at = r.fetched_at
                cur.soc_end = r.soc_percent
                cur.duration_min = int(
                    (r.fetched_at - cur.started_at).total_seconds() // 60
                )
        else:
            if cur is not None:
                out.append(cur)
                cur = None

        prev = r

    if cur is not None:
        out.append(cur)

    # Drop sub-2% blips: at integer-SoC resolution a 1% step is mostly
    # quantization noise / slow background loss, not a discrete event.
    candidates = [d for d in out if d.soc_drop >= 2]
    return _classify(candidates)


# Climate states that mean "the HVAC is (about to be) doing work" — see
# c3._CLIMA. climate_active alone misses the spin-up polls, where energy already
# flows but is_active is still False.
_CLIMATE_ACTIVE_STATES = {
    "starting", "starting (delay)", "external power", "active",
    "active + pre-cleaning", "heater active",
}


def _classify(drains: list[IdleDrain]) -> list[IdleDrain]:
    """Reject drives, then fill cause + parked_for_min for the real drains.

    Decision order, per drain, from the C3 rows in a tight ±10 min window:

    1. **Moved** (GPS displacement > MOVE_KM) → a drive → drop. A continuous
       drive's arrival fix lands within ±pad of where the SoC stops falling, so
       the tight window catches it without a greedy look-ahead (which would
       wrongly absorb a *later, separate* trip).
    2. **C3-active** (climate/air running) → real cabin conditioning, kept
       regardless of how recently the car parked.
    3. **No park stamp** (no C3 GPS) → physics backstop: a drive-level implied
       power is a missed drive (drop); otherwise keep, unattributed.
    4. **Just parked** (parked_for < SETTLE_MIN) → the SoC is settling after a
       drive, or the GPS froze mid-drive — not a real parked drain → drop.
    5. Otherwise a genuine deep-park draw → conditioning (kW-level) / vampire.
    """
    if not drains:
        return []

    pad = timedelta(minutes=10)
    kept: list[IdleDrain] = []

    with Session(get_engine()) as s:
        for d in drains:
            rows = list(
                s.exec(
                    select(C3Snapshot)
                    .where(C3Snapshot.fetched_at >= d.started_at - pad)
                    .where(C3Snapshot.fetched_at <= d.ended_at + pad)
                    .order_by(C3Snapshot.fetched_at.asc())
                ).all()
            )

            if _moved(rows):
                continue  # (1) a drive masquerading as a parked drain

            if any(
                r.climate_active or (r.climate_running in _CLIMATE_ACTIVE_STATES)
                for r in rows
            ):
                d.cause = "climate"  # (2)
                d.parked_for_min = _parked_for(rows, d)
                kept.append(d)
                continue
            if any(r.air_running for r in rows):
                d.cause = "air"  # (2)
                d.parked_for_min = _parked_for(rows, d)
                kept.append(d)
                continue

            parked_for = _parked_for(rows, d)
            if parked_for is None:
                # (3) No GPS/park stamp to vouch for standstill → trust physics.
                if d.implied_kw > DRIVE_KW:
                    continue
                kept.append(d)  # plausible but unattributable → cause stays None
                continue
            if parked_for < SETTLE_MIN:
                continue  # (4) post-drive settling / mid-drive freeze
            # (5) genuine parked draw
            d.parked_for_min = parked_for
            d.cause = "conditioning" if d.implied_kw >= CONDITIONING_KW else "vampire"
            kept.append(d)

    return kept


def _moved(rows: list[C3Snapshot]) -> bool:
    """True if any GPS fix in the window sits more than MOVE_KM from the first —
    the car was demonstrably somewhere else, i.e. it drove."""
    ref = next((r for r in rows if r.lat is not None and r.lng is not None), None)
    if ref is None:
        return False
    return any(
        r.lat is not None and r.lng is not None
        and haversine_km(ref.lat, ref.lng, r.lat, r.lng) > MOVE_KM
        for r in rows
    )


def _parked_for(rows: list[C3Snapshot], d: IdleDrain) -> int | None:
    """Minutes already parked when the drain started, from the C3 parked-fix
    timestamp (stamped when the car parks and held until it next moves). Scans
    for the first row that actually carries one — not every C3 row does."""
    ts = next((r.parked_ts or r.gps_ts for r in rows if (r.parked_ts or r.gps_ts)),
              None)
    if not ts:
        return None
    parked_at = datetime.fromtimestamp(ts, UTC).replace(tzinfo=None)
    if parked_at > d.started_at:
        return None
    return int((d.started_at - parked_at).total_seconds() // 60)
