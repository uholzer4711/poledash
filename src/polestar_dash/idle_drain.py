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
DRIVE_KW = 8.0  # implied power ceiling: no standing car sustains more (above ⇒
#                 missed drive or SoC quantization). Applied unconditionally.
CONDITIONING_KW = 2.0  # parked draw above this = active (battery/thermal) management
SETTLE_MIN = 30  # a drop within this long of parking is post-drive SoC settling /
#                  a mid-drive GPS freeze — not a real parked drain

# Every candidate SoC fall gets a `verdict`. The KEPT ones are genuine
# non-trip drains; the rest are excluded but still returned by
# diagnose_idle_drains() so the reasoning is fully inspectable.
#
# Kept (real drains):
#   "climate"      — C3 saw cabin pre-conditioning running
#   "air"          — C3 saw cabin air-cleaning running
#   "conditioning" — parked & climate idle, but kW-level draw (battery/thermal
#                    management or an in-car service), NOT tiny parasitic loss
#   "vampire"      — parked, low-power parasitic standby loss
#   "unknown"      — plausible parked draw but no C3 coverage to attribute it
# Excluded (not a parked drain):
#   "drive"        — GPS moved, or the odometer jumped right after ⇒ it was a trip
#   "implausible"  — implied draw above drive level ⇒ missed drive / SoC noise
#   "settling"     — drop < SETTLE_MIN after parking ⇒ post-drive SoC re-estimation
_ATTRIBUTED = {"climate", "air", "conditioning", "vampire"}
_KEPT = _ATTRIBUTED | {"unknown"}


@dataclass
class IdleDrain:
    started_at: datetime
    ended_at: datetime
    soc_start: int
    soc_end: int
    duration_min: int
    # Minutes the car had already been parked when the drain started (from the
    # C3 parked-fix timestamp) — parasitic loss shows up hours into a park,
    # conditioning right around departure/arrival.
    parked_for_min: int | None = None
    # True when the very next snapshot shows the odometer advanced — i.e. the
    # car drove off right after this SoC fall. With the odometer frozen *during*
    # the drop, that trailing jump is often the only proof the "drain" was really
    # the lead-in to (or the start of) a drive whose odometer hadn't refreshed.
    drove_after: bool = False
    # Final classification (set by _classify) — see the verdict taxonomy above.
    verdict: str = "unknown"
    # Where it happened: the C3 GPS fix in the window + a matching geofence label
    # (e.g. "Arbeit") — pure diagnostics, so you can see *where* a drop was kept
    # or excluded.
    lat: float | None = None
    lng: float | None = None
    place: str | None = None

    @property
    def cause(self) -> str | None:
        """Attributed cause when this is a real, named drain; None for a
        kept-but-unattributed ('unknown') or an excluded candidate."""
        return self.verdict if self.verdict in _ATTRIBUTED else None

    @property
    def kept(self) -> bool:
        """True when this counts as a genuine non-trip drain."""
        return self.verdict in _KEPT

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


def _candidates(vin: str | None, days: int) -> list[IdleDrain]:
    """Build raw drain candidates (collapsed ≥2% non-charging SoC falls), before
    any drive/standby classification. Shared by detect + diagnose."""
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
                # The breaking sample advanced the odometer ⇒ the car drove off
                # right after the drop (its frozen odometer hid the drive).
                cur.drove_after = odo_advanced
                out.append(cur)
                cur = None

        prev = r

    if cur is not None:
        out.append(cur)

    # Drop sub-2% blips: at integer-SoC resolution a 1% step is mostly
    # quantization noise / slow background loss, not a discrete event.
    return [d for d in out if d.soc_drop >= 2]


def detect_idle_drains(vin: str | None = None, days: int = 30) -> list[IdleDrain]:
    """The genuine non-trip drains (drives/settling/noise excluded)."""
    return [d for d in _classify(_candidates(vin, days)) if d.kept]


def diagnose_idle_drains(vin: str | None = None, days: int = 30) -> dict:
    """Every candidate SoC fall with its verdict + a tally, so the reasoning
    behind what's kept and what's excluded is fully inspectable."""
    classified = _classify(_candidates(vin, days))
    tally: dict[str, int] = {}
    for d in classified:
        tally[d.verdict] = tally.get(d.verdict, 0) + 1
    return {
        "candidates": sorted(classified, key=lambda d: d.started_at, reverse=True),
        "tally": tally,
        "kept": sum(1 for d in classified if d.kept),
        "excluded": sum(1 for d in classified if not d.kept),
    }


# Climate states that mean "the HVAC is (about to be) doing work" — see
# c3._CLIMA. climate_active alone misses the spin-up polls, where energy already
# flows but is_active is still False.
_CLIMATE_ACTIVE_STATES = {
    "starting", "starting (delay)", "external power", "active",
    "active + pre-cleaning", "heater active",
}


def _classify(drains: list[IdleDrain]) -> list[IdleDrain]:
    """Assign every candidate a `verdict` + location and return them ALL (the
    diagnostics view needs the excluded ones too; detect_idle_drains filters to
    `.kept`).

    Decision order, per drain, from the C3 rows in a tight ±10 min window:

    1. **Moved** (GPS displacement > MOVE_KM in window) → "drive".
    2. **C3-active** (climate/air running) → kept as "climate"/"air", real cabin
       conditioning even when a drive follows (the car warms up, then leaves).
    3. **Drove right after** (the trailing odometer jump) → "drive": the SoC fall
       was the lead-in to a drive whose odometer was still frozen — what a GPS
       that also froze at the departure point can't see, and what a *stale* park
       stamp would otherwise wave through.
    4. **Implausible power** (implied_kw > DRIVE_KW) → "drive"-class noise; no
       parked car sustains drive-level draw. Applied *unconditionally* (a
       long-looking `parked_for` can be a stale frozen stamp → no exemption).
    5. **Just parked** (parked_for < SETTLE_MIN) → "settling" (post-drive SoC
       re-estimation).
    6. **No park stamp** (no C3 GPS) → kept as "unknown" (unattributable).
    7. Otherwise a genuine deep-park draw → "conditioning" (kW-level) / "vampire".
    """
    if not drains:
        return []

    from .journey_import import get_address_geofences

    pad = timedelta(minutes=10)
    geofences = get_address_geofences()

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
            d.parked_for_min = _parked_for(rows, d)
            _attach_place(d, rows, geofences)

            if _moved(rows):
                d.verdict = "drive"  # (1)
            elif any(
                r.climate_active or (r.climate_running in _CLIMATE_ACTIVE_STATES)
                for r in rows
            ):
                d.verdict = "climate"  # (2)
            elif any(r.air_running for r in rows):
                d.verdict = "air"  # (2)
            elif d.drove_after:
                d.verdict = "drive"  # (3) drop merged into a drive (frozen odo)
            elif d.implied_kw > DRIVE_KW:
                d.verdict = "implausible"  # (4) impossible for a standing car
            elif d.parked_for_min is None:
                d.verdict = "unknown"  # (6) plausible but unattributable
            elif d.parked_for_min < SETTLE_MIN:
                d.verdict = "settling"  # (5) post-drive SoC re-estimation
            else:  # (7) genuine deep-park draw
                d.verdict = (
                    "conditioning" if d.implied_kw >= CONDITIONING_KW else "vampire"
                )

    return drains


def _attach_place(d: IdleDrain, rows: list[C3Snapshot], geofences: dict) -> None:
    """Stamp the drain with the first GPS fix in its window + a geofence label."""
    from .journey_import import geo_label_for

    fix = next((r for r in rows if r.lat is not None and r.lng is not None), None)
    if fix is None:
        return
    d.lat, d.lng = fix.lat, fix.lng
    d.place = geo_label_for(fix.lat, fix.lng, geofences)


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
