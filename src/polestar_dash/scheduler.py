from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlmodel import Session

from .backup import run_backup
from .config import get_settings
from .db.models import FetchError, RawSnapshot, VehicleSnapshot
from .db.session import get_engine
from .mqtt import schedule_publish
from .notify import notify
from .polestar.client import PolestarClient
from .polestar.fetcher import Snapshot, fetch_snapshot
from .anomalies import run_anomaly_checks
from .sessions import CHARGING_STATES, update_session
from .telegram import telegram_send

log = logging.getLogger(__name__)

JOB_ID = "polestar-poll"


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
            title=f"Charging finished — {snap.soc_percent}%",
            body=(
                f"Reached {snap.soc_percent}% (≈ {snap.range_km} km).\n"
                f"Session: {closed_session.soc_start}→{closed_session.soc_end}%, "
                f"peak {(closed_session.peak_power_w or 0)/1000:.1f} kW, "
                f"~{closed_session.energy_kwh or 0:.1f} kWh."
            ),
            vin=snap.vin,
            priority="high",
            tags=["battery", "zap"],
        )
        telegram_send(
            f"⚡ <b>Charging finished</b> — {snap.soc_percent}% (~{snap.range_km} km)\n"
            f"{closed_session.soc_start}→{closed_session.soc_end}%, "
            f"peak {(closed_session.peak_power_w or 0)/1000:.1f} kW, "
            f"~{closed_session.energy_kwh or 0:.1f} kWh."
        )

    # Low SoC (once per day at most)
    if snap.soc_percent is not None and snap.soc_percent <= s.notify_low_soc:
        day_key = snap.fetched_at.strftime("%Y-%m-%d")
        notify(
            kind="low_soc",
            key=f"{snap.vin}-{day_key}",
            title=f"Low battery — {snap.soc_percent}%",
            body=f"Only {snap.range_km or '?'} km of range left.",
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
            title="Service due soon",
            body=(
                f"{snap.days_to_service} days or {snap.distance_to_service_km or '?'} km "
                f"to next service."
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
        if not client.vins:
            log.warning("poll_once: no VINs known, skipping")
            return

        for vin in client.vins:
            try:
                snap = await fetch_snapshot(client, vin)
            except Exception as e:  # noqa: BLE001
                log.exception("Fetch failed for VIN %s", vin)
                with Session(get_engine()) as s:
                    s.add(FetchError(occurred_at=datetime.utcnow(), vin=vin, message=str(e)))
                    s.commit()
                continue

            # Each post-fetch step is isolated — a crash in notify or MQTT
            # should not lose the snapshot or break the poll cadence.
            try:
                _persist(snap)
            except Exception:  # noqa: BLE001
                log.exception("Persist failed for VIN %s", vin)
            try:
                closed = update_session(snap)
            except Exception:  # noqa: BLE001
                log.exception("update_session failed for VIN %s", vin)
                closed = None
            try:
                _maybe_notify(snap, closed)
            except Exception:  # noqa: BLE001
                log.exception("Notify failed for VIN %s", vin)
            try:
                schedule_publish(snap)
            except Exception:  # noqa: BLE001
                log.exception("MQTT publish failed for VIN %s", vin)
            try:
                run_anomaly_checks(snap)
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
    next_run = datetime.now() + timedelta(minutes=interval)
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


def build_scheduler(client: PolestarClient, interval_min: int) -> AsyncIOScheduler:
    global _SCHEDULER
    sched = AsyncIOScheduler()
    sched.add_job(
        poll_once,
        DateTrigger(run_date=datetime.now()),  # immediately
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
    _SCHEDULER = sched
    return sched
