from __future__ import annotations

from types import SimpleNamespace

from polestar_dash import notify as notify_mod
from polestar_dash.notify import notify


def _settings():
    # Only ntfy is "configured"; email/telegram disabled.
    return SimpleNamespace(
        ntfy_topic="t", ntfy_url="http://x", ntfy_username=None, ntfy_password=None,
        smtp_host=None, smtp_to=None,
        telegram_bot_token=None, telegram_chat_id=None,
        whatsapp_url=None, whatsapp_api_key=None, whatsapp_to=None,
    )


def test_failed_channel_is_not_marked_sent_and_backfills(tmp_db, monkeypatch):
    """A transient failure must NOT mark the event delivered; the next call for
    the same (kind,key) retries, and once it succeeds further calls dedup."""
    monkeypatch.setattr(notify_mod, "get_settings", _settings)

    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ntfy down")

    monkeypatch.setattr(notify_mod, "_send_ntfy", flaky)

    notify(kind="t", key="k1", title="x", body="y")  # fails → no row
    assert calls["n"] == 1
    notify(kind="t", key="k1", title="x", body="y")  # retries → succeeds, records
    assert calls["n"] == 2
    notify(kind="t", key="k1", title="x", body="y")  # deduped → not called again
    assert calls["n"] == 2
