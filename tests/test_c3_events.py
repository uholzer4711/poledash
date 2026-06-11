"""Tests for the persisted C3 event streams: per-bulb light warnings
(LightWarningEvent) and the car-reported OTA state (C3SoftwareEvent)."""

from __future__ import annotations

from sqlmodel import Session, select

from polestar_dash.db.models import C3Snapshot, C3SoftwareEvent, LightWarningEvent
from polestar_dash.util import utcnow


def _row(**kw) -> C3Snapshot:
    base = dict(vin="VIN1", fetched_at=utcnow())
    base.update(kw)
    return C3Snapshot(**base)


def test_record_light_events_transitions(tmp_db):
    from polestar_dash.db.session import get_engine
    from polestar_dash.scheduler import _record_light_events

    prev = _row(any_light_failure=False, failed_lights=None)
    row1 = _row(any_light_failure=True, failed_lights="DRL R,brake L")
    _record_light_events(row1, prev)

    # One bulb recovers → exactly one "cleared" event.
    row2 = _row(any_light_failure=True, failed_lights="DRL R")
    _record_light_events(row2, row1)

    # Health section missing (all-None row) → no fake "cleared" events.
    row3 = _row(any_light_failure=None, failed_lights=None)
    _record_light_events(row3, row2)

    # Unchanged state → no new rows.
    _record_light_events(row2, row2)

    with Session(get_engine()) as s:
        events = list(s.exec(select(LightWarningEvent).order_by(LightWarningEvent.id)).all())
    assert [(e.lamp, e.event) for e in events] == [
        ("DRL R", "fault"),
        ("brake L", "fault"),
        ("brake L", "cleared"),
    ]


def test_record_c3_software_event_dedups_per_state(tmp_db):
    from polestar_dash.db.session import get_engine
    from polestar_dash.software import record_c3_software_event

    sw = {"new_version": "4.2.13", "state_code": 2, "state": "downloading",
          "name": "Update 4.2.13", "notes": "fixes"}
    record_c3_software_event("VIN1", sw)
    record_c3_software_event("VIN1", sw)  # same (version, state) → no new row
    record_c3_software_event("VIN1", {**sw, "state_code": 9, "state": "installed"})
    record_c3_software_event("VIN1", {"new_version": ""})  # no version → ignored

    with Session(get_engine()) as s:
        events = list(s.exec(select(C3SoftwareEvent).order_by(C3SoftwareEvent.id)).all())
    assert [(e.version, e.state_code) for e in events] == [("4.2.13", 2), ("4.2.13", 9)]
    assert events[0].state == "downloading"
