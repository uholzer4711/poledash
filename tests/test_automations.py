"""Safety guarantee: locking the car must never run unattended.

Incident 2026-08-12 — the CAR_UNLOCKED_AFTER_TIME rule was configured
"autonom" and locked the car while the owner's key was inside, stranding
them. There is no C3 signal for "key/phone is in the cabin", so this rule
must always require a human tap, regardless of what's stored in settings.
"""

from __future__ import annotations

from datetime import UTC, datetime

from polestar_dash.db.models import C3Snapshot


def _evening_unlocked_row(**kw) -> C3Snapshot:
    # 2026-01-15 21:00 UTC = 22:00 Europe/Vienna in January (no DST) — well
    # past the rule's default 20:00 threshold.
    base = dict(
        vin="VIN1",
        fetched_at=datetime(2026, 1, 15, 21, 0, tzinfo=UTC),
        is_locked=False,
        speed=0,
    )
    base.update(kw)
    return C3Snapshot(**base)


async def test_lock_rule_never_runs_autonomously_even_if_stored_that_way(
    tmp_db, monkeypatch
):
    from polestar_dash import automations
    from polestar_dash.app_settings import set_automation_config

    # Simulate stale/legacy data (or a direct DB edit) that still says
    # "autonom" for the lock rule.
    set_automation_config(
        "car_unlocked_after_time", enabled=True, mode="autonom", after=20
    )

    called = {"run_command": False}

    async def _fail_if_called(*a, **kw):
        called["run_command"] = True
        raise AssertionError("lock must never run autonomously")

    suggested = {}

    async def _fake_suggest(rule, row, ctx):
        suggested["rule_id"] = rule.id

    monkeypatch.setattr(automations, "run_command", _fail_if_called)
    monkeypatch.setattr(automations, "_emit_suggestion", _fake_suggest)

    await automations.run_automations(_evening_unlocked_row())

    assert called["run_command"] is False
    assert suggested["rule_id"] == "car_unlocked_after_time"


def test_lock_rule_flagged_not_autonomous():
    from polestar_dash.automations import CAR_UNLOCKED_AFTER_TIME

    assert CAR_UNLOCKED_AFTER_TIME.allow_autonomous is False


def test_all_automation_configs_clamps_stale_autonom_mode(tmp_db):
    from polestar_dash.app_settings import set_automation_config
    from polestar_dash.automations import all_automation_configs

    set_automation_config("car_unlocked_after_time", mode="autonom")
    cfg = next(
        c for c in all_automation_configs() if c["id"] == "car_unlocked_after_time"
    )
    assert cfg["mode"] == "vorschlag"
    assert cfg["allow_autonomous"] is False


def test_api_rejects_autonom_for_lock_rule(tmp_db):
    from fastapi.testclient import TestClient

    from polestar_dash.main import app

    client = TestClient(app)
    r = client.post(
        "/api/app-settings/automation/car_unlocked_after_time?mode=autonom"
    )
    assert r.status_code == 400
