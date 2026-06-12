"""Tests for the C3 "vehicle connection lost" alert: it fires once when the C3
login degrades (every section PERMISSION_DENIED → all-`None` rows persist), only
after the outage persists past a transient throttle, and not for a car that was
never connected."""

from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session

from polestar_dash.db.models import C3Snapshot
from polestar_dash.util import utcnow

BASE = utcnow() - timedelta(hours=1)


def _persist(rows: list[C3Snapshot]) -> None:
    from polestar_dash.db.session import get_engine

    with Session(get_engine()) as s:
        for r in rows:
            s.add(r)
        s.commit()


def _connected(i: int) -> C3Snapshot:
    return C3Snapshot(vin="VIN1", fetched_at=BASE + timedelta(minutes=5 * i),
                      is_locked=True, door_fl="closed", lat=46.0, lng=14.0)


def _stateless(i: int) -> C3Snapshot:
    # The shape a degraded C3 login leaves behind: row stored, all state None.
    return C3Snapshot(vin="VIN1", fetched_at=BASE + timedelta(minutes=5 * i))


def test_disconnect_alerts_once_after_persisting(tmp_db, monkeypatch):
    import polestar_dash.c3_anomalies as ca

    captured: list[dict] = []
    monkeypatch.setattr(ca, "notify", lambda **kw: captured.append(kw))

    # Three good polls, then the login degrades. (Pass a freshly built instance
    # to the check — the scheduler hands in the refreshed, expunged poll row, not
    # a commit-expired ORM instance.)
    _persist([_connected(0), _connected(1), _connected(2)])

    # First stateless poll → transient, no alert yet.
    _persist([_stateless(3)])
    ca._check_c3_disconnected(_stateless(3))
    assert captured == []

    # Second consecutive stateless poll → confirmed disconnect, one alert.
    _persist([_stateless(4)])
    ca._check_c3_disconnected(_stateless(4))
    assert len(captured) == 1
    alert = captured[0]
    assert alert["kind"] == "c3_disconnected"
    assert alert["priority"] == "high"

    # Third stateless poll → same episode → same dedup key (real notify() would
    # suppress it via NotificationLog), so no second distinct alert.
    _persist([_stateless(5)])
    ca._check_c3_disconnected(_stateless(5))
    assert {c["key"] for c in captured} == {alert["key"]}


def test_reconnect_clears_once_after_outage(tmp_db, monkeypatch):
    import polestar_dash.c3_anomalies as ca

    captured: list[dict] = []
    monkeypatch.setattr(ca, "notify", lambda **kw: captured.append(kw))

    # Connected, then a confirmed outage (>= 2 stateless polls), then back.
    _persist([_connected(0), _connected(1)])
    _persist([_stateless(2), _stateless(3)])

    # First connected poll after the outage → exactly one all-clear.
    _persist([_connected(4)])
    ca._check_c3_reconnected(_connected(4))
    assert len(captured) == 1
    assert captured[0]["kind"] == "c3_reconnected"

    # A subsequent connected poll has a connected predecessor → no re-clear.
    _persist([_connected(5)])
    ca._check_c3_reconnected(_connected(5))
    assert len(captured) == 1


def test_no_reconnect_clear_for_transient_blip(tmp_db, monkeypatch):
    import polestar_dash.c3_anomalies as ca

    captured: list[dict] = []
    monkeypatch.setattr(ca, "notify", lambda **kw: captured.append(kw))

    # One stateless poll (below the disconnect threshold) → no outage, no clear.
    _persist([_connected(0), _stateless(1)])
    _persist([_connected(2)])
    ca._check_c3_reconnected(_connected(2))
    assert captured == []


def test_no_alert_when_connected(tmp_db, monkeypatch):
    import polestar_dash.c3_anomalies as ca

    captured: list[dict] = []
    monkeypatch.setattr(ca, "notify", lambda **kw: captured.append(kw))

    _persist([_connected(0), _connected(1)])
    ca._check_c3_disconnected(_connected(1))
    assert captured == []


def test_no_alert_when_never_connected(tmp_db, monkeypatch):
    import polestar_dash.c3_anomalies as ca

    captured: list[dict] = []
    monkeypatch.setattr(ca, "notify", lambda **kw: captured.append(kw))

    # Only stateless rows on record (e.g. never paired) → not a live "drop".
    _persist([_stateless(i) for i in range(4)])
    ca._check_c3_disconnected(_stateless(3))
    assert captured == []
