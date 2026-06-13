"""Tests for the C3 "vehicle connection lost / restored" alerts.

The detector is time-based and whole-table (not a fixed recent-row window): it
fires once when the last good C3 fix is older than the disconnect threshold, and
the all-clear fires once on the first good poll whose gap to the previous good
fix exceeded that threshold. Whole-table is what makes it survive an app restart
mid-outage — the old recent-window lookback went blind the moment every row it
could see was stateless. Stateless polls are no longer persisted, so detection
keys purely off the timestamps of the *good* fixes."""

from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session

from polestar_dash.c3_anomalies import disconnect_gap
from polestar_dash.db.models import C3Snapshot
from polestar_dash.util import utcnow

BASE = utcnow() - timedelta(hours=3)
GAP = disconnect_gap()
PAST = GAP + timedelta(minutes=10)    # comfortably past the gap → confirmed outage
WITHIN = GAP - timedelta(minutes=5)   # still a transient blip below the threshold


def _persist(rows: list[C3Snapshot]) -> None:
    from polestar_dash.db.session import get_engine

    with Session(get_engine()) as s:
        for r in rows:
            s.add(r)
        s.commit()


def _good(dt) -> C3Snapshot:
    """A poll that carried real vehicle state."""
    return C3Snapshot(vin="VIN1", fetched_at=dt,
                      is_locked=True, door_fl="closed", lat=46.0, lng=14.0)


def _blank(dt) -> C3Snapshot:
    """Legacy all-None row a degraded login used to leave behind (we no longer
    persist these, but the detector must still ignore them if present)."""
    return C3Snapshot(vin="VIN1", fetched_at=dt)


def test_disconnect_alerts_once_after_gap(tmp_db, monkeypatch):
    import polestar_dash.c3_anomalies as ca

    captured: list[dict] = []
    monkeypatch.setattr(ca, "notify", lambda **kw: captured.append(kw))

    _persist([_good(BASE), _good(BASE + timedelta(minutes=5))])
    last_good = BASE + timedelta(minutes=5)

    # Still within the threshold → transient throttle, no alert yet.
    ca.check_c3_disconnected("VIN1", last_good + WITHIN)
    assert captured == []

    # Past the threshold → one confirmed disconnect alert.
    ca.check_c3_disconnected("VIN1", last_good + PAST)
    assert len(captured) == 1
    assert captured[0]["kind"] == "c3_disconnected"
    assert captured[0]["priority"] == "high"

    # Hours later, still down → same episode → same dedup key (real notify()
    # would suppress it via NotificationLog).
    ca.check_c3_disconnected("VIN1", last_good + timedelta(hours=2))
    assert {c["key"] for c in captured} == {captured[0]["key"]}


def test_disconnect_fires_across_restart(tmp_db, monkeypatch):
    """Regression: a single old good fix and nothing since (the shape after an
    app restart 4 h into an outage). The old recent-row lookback never saw a
    connected row and stayed silent; the whole-table query still fires."""
    import polestar_dash.c3_anomalies as ca

    captured: list[dict] = []
    monkeypatch.setattr(ca, "notify", lambda **kw: captured.append(kw))

    _persist([_good(BASE)])
    ca.check_c3_disconnected("VIN1", BASE + timedelta(hours=25))
    assert len(captured) == 1
    assert captured[0]["kind"] == "c3_disconnected"


def test_reconnect_clears_once_after_outage(tmp_db, monkeypatch):
    import polestar_dash.c3_anomalies as ca

    captured: list[dict] = []
    monkeypatch.setattr(ca, "notify", lambda **kw: captured.append(kw))

    _persist([_good(BASE), _good(BASE + timedelta(minutes=5))])
    # Outage: no good polls for well past the threshold, then back. (Pass a fresh
    # instance to the check — the scheduler hands in the refreshed/expunged poll
    # row, not a commit-expired ORM instance.)
    back_at = BASE + timedelta(minutes=5) + PAST
    _persist([_good(back_at)])
    ca._check_c3_reconnected(_good(back_at))
    assert len(captured) == 1
    assert captured[0]["kind"] == "c3_reconnected"

    # A subsequent good poll has a recent good predecessor → no re-clear.
    nxt_at = back_at + timedelta(minutes=5)
    _persist([_good(nxt_at)])
    ca._check_c3_reconnected(_good(nxt_at))
    assert len(captured) == 1


def test_no_reconnect_clear_for_transient_blip(tmp_db, monkeypatch):
    import polestar_dash.c3_anomalies as ca

    captured: list[dict] = []
    monkeypatch.setattr(ca, "notify", lambda **kw: captured.append(kw))

    # Gap below the threshold → not a confirmed outage → no all-clear.
    _persist([_good(BASE)])
    back_at = BASE + WITHIN
    _persist([_good(back_at)])
    ca._check_c3_reconnected(_good(back_at))
    assert captured == []


def test_no_alert_when_connected(tmp_db, monkeypatch):
    import polestar_dash.c3_anomalies as ca

    captured: list[dict] = []
    monkeypatch.setattr(ca, "notify", lambda **kw: captured.append(kw))

    _persist([_good(BASE)])
    ca.check_c3_disconnected("VIN1", BASE + timedelta(minutes=5))
    assert captured == []


def test_no_alert_when_never_connected(tmp_db, monkeypatch):
    import polestar_dash.c3_anomalies as ca

    captured: list[dict] = []
    monkeypatch.setattr(ca, "notify", lambda **kw: captured.append(kw))

    # Only blank rows on record (e.g. never paired) → no good fix → not a drop.
    _persist([_blank(BASE + timedelta(minutes=5 * i)) for i in range(4)])
    ca.check_c3_disconnected("VIN1", BASE + timedelta(hours=25))
    assert captured == []
