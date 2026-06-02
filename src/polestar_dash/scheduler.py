from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import UTC, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlmodel import Session

from .anomalies import run_anomaly_checks
from .backup import run_backup
from .c3_anomalies import check_rain_windows, run_c3_anomaly_checks
from .config import get_settings
from .db.models import C3Snapshot, FetchError, RawC3Snapshot, RawSnapshot, VehicleSnapshot
from .db.session import get_engine
from .digest import weekly_digest
from .i18n import t
from .mqtt import schedule_publish, schedule_publish_c3
from .notify import notify
from .polestar.c3 import c3_dict_to_row, get_c3_snapshot
from .polestar.client import PolestarClient
from .polestar.fetcher import Snapshot, fetch_snapshot
from .sessions import CHARGING_STATES, previous_snapshot, update_session
from .software import check_software_updates, record_installed_version
from .util import haversine_km, utcnow

# Retention for the raw-JSON safety net. Structured rows in VehicleSnapshot
# stay forever; the verbatim API payloads we keep just long enough to debug
# field-mapping gaps and to recover from a bad migration.
RAW_RETENTION_DAYS = 90

log = logging.getLogger(__name__)

JOB_ID = "polestar-poll"
C3_JOB_ID = "c3-poll"


def _persist(snap: Snapshot) -> None:
    # Project the Snapshot dataclass onto VehicleSnapshot's columns. Tolerant
    # to Snapshot growing new fields the DB doesn't know yet — they're just
    # dropped instead of raising TypeError on construction.
    db_fields = set(VehicleSnapshot.model_fields.keys())
    row_data = {
        f.name: getattr(snap, f.name)
        for f in dataclasses.fields(snap)
        if f.name != "raw" and f.name in db_fields
    }
    with Session(get_engine()) as s:
        s.add(VehicleSnapshot(**row_data))
        s.add(RawSnapshot(fetched_at=snap.fetched_at, vin=snap.vin, payload=snap.raw))
        s.commit()


def _maybe_notify(snap: Snapshot, closed_session) -> None:  # noqa: ANN001
    s = get_settings()

    # Charge complete
    if closed_session is not None:
        notify(
            kind="charge_complete",
            key=f"session-{closed_session.id}",
            title=t("Charging finished — {soc}%", soc=snap.soc_percent),
            body=t(
                "Reached {soc}% ({range} km).\n"
                "Session: {start} → {end}%, peak {peak:.1f} kW, ~{kwh:.1f} kWh.",
                soc=snap.soc_percent,
                range=snap.range_km,
                start=closed_session.soc_start,
                end=closed_session.soc_end,
                peak=(closed_session.peak_power_w or 0) / 1000,
                kwh=closed_session.energy_kwh or 0,
            ),
            vin=snap.vin,
            priority="high",
            tags=["battery", "zap"],
        )

    # Low SoC (once per day at most)
    if snap.soc_percent is not None and snap.soc_percent <= s.notify_low_soc:
        day_key = snap.fetched_at.strftime("%Y-%m-%d")
        notify(
            kind="low_soc",
            key=f"{snap.vin}-{day_key}",
            title=t("Low battery — {soc}%", soc=snap.soc_percent),
            body=t("Only {range} km of range left.", range=snap.range_km or "?"),
            vin=snap.vin,
            priority="high",
            tags=["warning", "battery"],
        )

    # Service due soon
    if (
        snap.days_to_service is not None
        and snap.days_to_service <= s.notify_service_days
    ):
        notify(
            kind="service_due",
            key=f"{snap.vin}-d{snap.days_to_service}",
            title=t("Service due soon"),
            body=t(
                "{days} days or {dist} km to next service.",
                days=snap.days_to_service,
                dist=snap.distance_to_service_km or "?",
            ),
            vin=snap.vin,
            tags=["wrench"],
        )


async def poll_once(client: PolestarClient) -> None:
    """Single poll cycle. Always re-schedules itself — any exception in
    persistence/notify/MQTT must not kill the loop (we lost ~1h once because
    a logging bug in update_session crashed poll_once *before* _reschedule)."""
    any_charging = False
    try:
        # If the startup login failed (or dropped), `connect()` never populated
        # the client. Retry here so a transient outage at boot doesn't leave the
        # loop polling-and-failing forever until a manual restart.
        if not client.connected:
            try:
                await client.connect()
            except Exception:  # noqa: BLE001
                log.exception("poll_once: reconnect failed — will retry next interval")
                return

        if not client.vins:
            log.warning("poll_once: no VINs known, skipping")
            return

        for vin in client.vins:
            try:
                snap = await fetch_snapshot(client, vin)
            except Exception as e:  # noqa: BLE001
                log.exception("Fetch failed for VIN %s", vin)
                with Session(get_engine()) as s:
                    s.add(FetchError(occurred_at=utcnow(), vin=vin, message=str(e)))
                    s.commit()
                continue

            # Each post-fetch step is isolated — a crash in notify or MQTT
            # should not lose the snapshot or break the poll cadence.
            # Read the previous snapshot *before* persisting the new one so
            # both update_session and run_anomaly_checks see the same view,
            # and so we don't repeat the indexed lookup three times.
            try:
                with Session(get_engine()) as _s:
                    prev = previous_snapshot(_s, snap.vin, snap.fetched_at)
                    if prev is not None:
                        _s.expunge(prev)
            except Exception:  # noqa: BLE001
                log.exception("previous_snapshot lookup failed for VIN %s", vin)
                prev = None
            try:
                _persist(snap)
            except Exception:  # noqa: BLE001
                log.exception("Persist failed for VIN %s", vin)
            try:
                closed = update_session(snap, prev=prev)
            except Exception:  # noqa: BLE001
                log.exception("update_session failed for VIN %s", vin)
                closed = None
            # Records installed-version changes + may notify (blocking I/O) —
            # offload like the other notify steps.
            try:
                await asyncio.to_thread(record_installed_version, snap)
            except Exception:  # noqa: BLE001
                log.exception("Software version tracking failed for VIN %s", vin)
            # notify() and the anomaly checks do blocking network I/O (ntfy
            # HTTP, SMTP up to 15 s, Telegram) — run them off the event loop so
            # they can't stall the poll cadence or anything else on the loop.
            try:
                await asyncio.to_thread(_maybe_notify, snap, closed)
            except Exception:  # noqa: BLE001
                log.exception("Notify failed for VIN %s", vin)
            try:
                schedule_publish(snap)
            except Exception:  # noqa: BLE001
                log.exception("MQTT publish failed for VIN %s", vin)
            try:
                await asyncio.to_thread(run_anomaly_checks, snap, prev=prev)
            except Exception:  # noqa: BLE001
                log.exception("Anomaly checks failed for VIN %s", vin)

            if snap.charging_status in CHARGING_STATES:
                any_charging = True

            log.info(
                "Stored snapshot for VIN %s (SoC=%s%%, status=%s)",
                vin,
                snap.soc_percent,
                snap.charging_status,
            )
    except Exception:  # noqa: BLE001
        log.exception("poll_once: unexpected error — rescheduling anyway")
    finally:
        _reschedule(client, charging=any_charging)


def _reschedule(client: PolestarClient, *, charging: bool) -> None:
    """Schedule the next single-shot poll at fast/slow cadence."""
    s = get_settings()
    sched: AsyncIOScheduler | None = _SCHEDULER
    if sched is None:
        return
    interval = s.fast_poll_interval_min if charging else s.poll_interval_min
    next_run = utcnow() + timedelta(minutes=interval)
    sched.add_job(
        poll_once,
        DateTrigger(run_date=next_run),
        args=[client],
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    log.info("Next poll at %s (%s mode)", next_run.isoformat(), "fast" if charging else "slow")


_SCHEDULER: AsyncIOScheduler | None = None


def _latest_c3(vin: str) -> C3Snapshot | None:
    from sqlmodel import select

    with Session(get_engine()) as s:
        row = s.exec(
            select(C3Snapshot)
            .where(C3Snapshot.vin == vin)
            .order_by(C3Snapshot.fetched_at.desc())
            .limit(1)
        ).first()
        if row is not None:
            s.expunge(row)
        return row


async def c3_poll_once() -> None:
    """Single C3 (Volvo Cloud Connectivity) poll. Mirrors poll_once's defensive
    shape: every post-fetch step is isolated and the reschedule lives in a
    finally, so a C3 outage never kills its own loop and never touches the main
    pypolestar poll. Deliberately slower + staggered (shared rate limit)."""
    charging = False
    driving = False
    try:
        data = await get_c3_snapshot(force=True)
        if not data.get("ok"):
            log.warning("C3 poll: backend error — %s", data.get("error"))
            return
        row_data = c3_dict_to_row(data)
        if not row_data or not row_data.get("vin"):
            log.warning("C3 poll: no usable data (no VIN)")
            return
        vin = row_data["vin"]
        charging = bool((data.get("battery") or {}).get("is_charging"))

        try:
            prev = _latest_c3(vin)
        except Exception:  # noqa: BLE001
            log.exception("C3 previous-row lookup failed for VIN %s", vin)
            prev = None

        row = C3Snapshot(fetched_at=utcnow(), **row_data)

        # Driving → poll faster so the GPS trail is dense enough to draw a route.
        # Detected from the car's own speed, or movement since the last fix.
        speed = row.speed or 0
        moved = (
            prev is not None
            and prev.lat is not None and prev.lng is not None
            and row.lat is not None and row.lng is not None
            and haversine_km(prev.lat, prev.lng, row.lat, row.lng) > 0.3
        )
        driving = speed >= 3 or moved
        try:
            with Session(get_engine()) as s:
                s.add(row)
                s.add(RawC3Snapshot(fetched_at=row.fetched_at, vin=vin, payload=data))
                s.commit()
                s.refresh(row)
                s.expunge(row)
        except Exception:  # noqa: BLE001
            log.exception("C3 persist failed for VIN %s", vin)
        try:
            await asyncio.to_thread(run_c3_anomaly_checks, row, prev=prev)
        except Exception:  # noqa: BLE001
            log.exception("C3 anomaly checks failed for VIN %s", vin)
        try:
            await check_rain_windows(row)
        except Exception:  # noqa: BLE001
            log.exception("C3 rain/windows check failed for VIN %s", vin)
        try:
            schedule_publish_c3(row)
        except Exception:  # noqa: BLE001
            log.exception("C3 MQTT publish failed for VIN %s", vin)
        log.info("Stored C3 snapshot for VIN %s (locked=%s, charging=%s)",
                 vin, row.is_locked, charging)
    except Exception:  # noqa: BLE001
        log.exception("c3_poll_once: unexpected error — rescheduling anyway")
    finally:
        _reschedule_c3(charging=charging, driving=driving)


def _reschedule_c3(*, charging: bool, driving: bool = False) -> None:
    s = get_settings()
    sched: AsyncIOScheduler | None = _SCHEDULER
    if sched is None or not s.c3_enabled:
        return
    if charging:
        interval = s.c3_fast_poll_interval_min
    elif driving:
        interval = s.c3_drive_poll_interval_min
    else:
        interval = s.c3_poll_interval_min
    next_run = utcnow() + timedelta(minutes=interval)
    sched.add_job(
        c3_poll_once,
        DateTrigger(run_date=next_run),
        id=C3_JOB_ID,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )


def _prune_raw_snapshots() -> None:
    """Delete RawSnapshot + RawC3Snapshot rows older than RAW_RETENTION_DAYS to
    keep the DB from growing unbounded (~20 KB/poll × every 2–10 min = >1 GB/year)."""
    cutoff = utcnow() - timedelta(days=RAW_RETENTION_DAYS)
    with Session(get_engine()) as s:
        result = s.execute(
            RawSnapshot.__table__.delete().where(RawSnapshot.fetched_at < cutoff)
        )
        result_c3 = s.execute(
            RawC3Snapshot.__table__.delete().where(RawC3Snapshot.fetched_at < cutoff)
        )
        s.commit()
    log.info("Pruned %s RawSnapshot + %s RawC3Snapshot rows older than %s days",
             getattr(result, "rowcount", "?"), getattr(result_c3, "rowcount", "?"),
             RAW_RETENTION_DAYS)


def build_scheduler(client: PolestarClient, interval_min: int) -> AsyncIOScheduler:
    global _SCHEDULER
    s = get_settings()
    # Pin to UTC — every timestamp we write uses utcnow(), and APScheduler
    # interprets naive datetimes in *its* timezone. Without this, deploying
    # the image with TZ=Europe/Vienna would offset every reschedule by 2h.
    sched = AsyncIOScheduler(timezone=UTC)
    sched.add_job(
        poll_once,
        DateTrigger(run_date=utcnow()),  # immediately
        args=[client],
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    sched.add_job(
        run_backup,
        CronTrigger(hour=3, minute=15),
        id="daily-backup",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    sched.add_job(
        _prune_raw_snapshots,
        CronTrigger(hour=3, minute=45),
        id="raw-retention",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    sched.add_job(
        weekly_digest,
        CronTrigger(day_of_week="sun", hour=17, minute=0),
        id="weekly-digest",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    # Software-update watch — twice a day (UTC 06:00 / 18:00 ≈ 08:00 / 20:00
    # Vienna). Scrapes the manual and emails when a newer version is listed.
    sched.add_job(
        check_software_updates,
        CronTrigger(hour="6,18", minute=0),
        id="software-update-check",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    # C3 poll — own loop, started staggered (half a C3 interval after the main
    # poll) so the two libraries don't hammer the shared account at the same
    # instant. Reschedules itself adaptively from c3_poll_once's finally block.
    if s.c3_enabled:
        sched.add_job(
            c3_poll_once,
            DateTrigger(run_date=utcnow() + timedelta(minutes=s.c3_poll_interval_min / 2)),
            id=C3_JOB_ID,
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    _SCHEDULER = sched
    return sched
