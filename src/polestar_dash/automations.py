"""Rule-based automation engine — acts on the car from C3 state + context.

Generic by design: a ``Rule`` pairs a trigger predicate with a C3 write command
and a per-rule config (enabled + mode). ``run_automations(row)`` is called from
``scheduler.c3_poll_once`` right after the fresh C3 fetch, so rules see current
state. Two execution modes (chosen per rule in settings):

* **autonom** — condition met → run the command, verify it, then notify the
  result ("closed the windows automatically ✓").
* **vorschlag** — condition met → push a one-tap suggestion (a /confirm link);
  nothing touches the car until the user taps it.

Defaults are conservative — rules are opt-in (disabled) and default to
*vorschlag*, because every command moves real hardware. The first (and, for
now, only) rule is *rain → close windows*; adding another is a few lines.

Relationship to ``c3_anomalies.check_rain_windows``: that stays the *alert*
(warn that it's raining on open windows). This is the *action* (close them).
They're independent toggles so a user can warn-only, act, or both.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlmodel import Session, select

from .app_settings import get_automation_config
from .c3_anomalies import _is_parked, _local_hour, _windows_open
from .config import get_settings
from .db.models import C3Snapshot, NotificationLog
from .db.session import get_engine
from .i18n import t
from .notify import notify
from .pending_actions import create_pending
from .polestar.c3_control import run_command
from .polestar.control_verify import verify_command
from .util import utcnow

log = logging.getLogger(__name__)


@dataclass
class Rule:
    id: str
    title: str  # i18n source string (shown in settings + confirm page)
    command: str
    # trigger(row, cfg) -> (should_fire, context). cfg is the rule's stored
    # config (enabled/mode/after); context feeds message templates.
    trigger: Callable[[C3Snapshot, dict], Awaitable[tuple[bool, dict]]]
    params: dict = field(default_factory=dict)
    default_enabled: bool = False
    default_mode: str = "vorschlag"
    # Time-triggered rules expose an "act from this local hour" dropdown in
    # settings; time_label is its i18n caption, default_after its default hour.
    time_label: str = ""
    default_after: int | None = None
    suggest_label: str = ""  # one-tap button text (i18n source)
    suggest_body: str = ""   # suggestion body (i18n source, may use {parts}/{after})
    acted_body: str = ""     # autonomous success body (i18n source)
    failed_body: str = ""    # autonomous "couldn't confirm" body (i18n source)


# --- triggers -----------------------------------------------------------------
async def _rain_windows_trigger(row: C3Snapshot, cfg: dict) -> tuple[bool, dict]:
    """Raining at the car while a window/sunroof is open, car parked."""
    if row.lat is None or row.lng is None:
        return False, {}
    open_labels = _windows_open(row)
    if not open_labels or not _is_parked(row):
        return False, {}
    from .weather import current_at

    wx = await current_at(row.lat, row.lng)
    if not wx or not wx.get("is_rain"):
        return False, {}
    return True, {"open_labels": open_labels}


async def _windows_after_time_trigger(row: C3Snapshot, cfg: dict) -> tuple[bool, dict]:
    """A window/sunroof is still open, car parked, and it's past the configured
    local hour (e.g. 21:00). For closing up at night regardless of weather."""
    after = cfg.get("after")
    if after is None:
        return False, {}
    open_labels = _windows_open(row)
    if not open_labels or not _is_parked(row):
        return False, {}
    if _local_hour(row) < after:
        return False, {}
    return True, {"open_labels": open_labels, "after": after}


# --- registry -----------------------------------------------------------------
RAIN_CLOSE_WINDOWS = Rule(
    id="rain_close_windows",
    title="Rain → close windows",
    command="windows_close",
    trigger=_rain_windows_trigger,
    default_enabled=False,
    default_mode="vorschlag",
    suggest_label="Close windows now",
    suggest_body="It started raining and these are open: {parts}. Close them?",
    acted_body="It started raining — closed the windows automatically.",
    failed_body="It started raining — tried to close the windows but couldn't confirm.",
)

WINDOWS_CLOSE_AFTER_TIME = Rule(
    id="windows_close_after_time",
    title="Windows still open in the evening → close",
    command="windows_close",
    trigger=_windows_after_time_trigger,
    default_enabled=False,
    default_mode="vorschlag",
    time_label="Close after",
    default_after=21,
    suggest_label="Close windows now",
    suggest_body="A window is still open ({parts}) after {after}:00. Close it?",
    acted_body="A window was still open in the evening — closed it automatically.",
    failed_body="Tried to close the windows but couldn't confirm.",
)

RULES: list[Rule] = [RAIN_CLOSE_WINDOWS, WINDOWS_CLOSE_AFTER_TIME]


def rule_by_id(rule_id: str) -> Rule | None:
    return next((r for r in RULES if r.id == rule_id), None)


def all_automation_configs() -> list[dict]:
    """{id, title, enabled, mode} per rule, for the settings frame."""
    return [
        {
            "id": r.id,
            "title": r.title,
            "time_label": r.time_label,
            **get_automation_config(
                r.id,
                default_enabled=r.default_enabled,
                default_mode=r.default_mode,
                default_after=r.default_after,
            ),
        }
        for r in RULES
    ]


# --- runtime ------------------------------------------------------------------
def _claim_once(kind: str, key: str, vin: str | None = None) -> bool:
    """Record (kind, key) and return True exactly once — a per-day action guard
    so a persistent condition (it keeps raining) acts/suggests only once, not on
    every poll. Reuses the NotificationLog table the notifier already dedups on."""
    with Session(get_engine()) as s:
        exists = s.exec(
            select(NotificationLog).where(
                NotificationLog.kind == kind, NotificationLog.key == key
            )
        ).first()
        if exists is not None:
            return False
        s.add(NotificationLog(sent_at=utcnow(), vin=vin, kind=kind, key=key))
        s.commit()
    return True


def _parts(ctx: dict) -> str:
    return ", ".join(t(lbl) for lbl in ctx.get("open_labels", []))


async def _run_autonomous(rule: Rule, row: C3Snapshot, ctx: dict) -> None:
    day = f"{row.vin}-{row.fetched_at:%Y-%m-%d}-auto-{rule.id}"
    if not _claim_once(f"auto_{rule.id}", day, row.vin):
        return  # already acted on this condition today
    res = await run_command(rule.command, rule.params)
    verdict = await verify_command(rule.command, rule.params)
    verified = verdict.get("verified")
    ok = verified is True or (verified is None and res.get("ok"))
    body = t(rule.acted_body, parts=_parts(ctx), after=ctx.get("after"))
    if not ok:
        body = t(rule.failed_body, parts=_parts(ctx), after=ctx.get("after"))
    if verified is True:
        body += " " + t("Confirmed.")
    await asyncio.to_thread(
        notify,
        kind=f"auto_{rule.id}_done",
        key=f"{day}-done",
        title=t("Automation acted"),
        body=body,
        vin=row.vin,
        priority="high",
        tags=["car"],
    )


async def _emit_suggestion(rule: Rule, row: C3Snapshot, ctx: dict) -> None:
    day = f"{row.vin}-{row.fetched_at:%Y-%m-%d}-suggest-{rule.id}"
    if not _claim_once(f"auto_{rule.id}_suggest", day, row.vin):
        return
    token = create_pending(rule.id, rule.command, rule.params, vin=row.vin)
    base = (get_settings().public_base_url or "").rstrip("/")
    url = f"{base}/confirm/{token}" if base else None
    body = t(rule.suggest_body, parts=_parts(ctx), after=ctx.get("after"))
    if not url:
        body += " " + t("Open poledash to confirm.")
    await asyncio.to_thread(
        notify,
        kind=f"auto_{rule.id}_suggest",
        key=day,
        title=t("Suggestion"),
        body=body,
        vin=row.vin,
        priority="high",
        tags=["car"],
        action_url=url,
        action_label=t(rule.suggest_label),
    )


async def run_automations(row: C3Snapshot) -> None:
    """Evaluate every enabled rule against the fresh snapshot. Each rule is
    isolated — one failing rule never blocks the others or the poll."""
    for rule in RULES:
        try:
            cfg = get_automation_config(
                rule.id,
                default_enabled=rule.default_enabled,
                default_mode=rule.default_mode,
                default_after=rule.default_after,
            )
            if not cfg["enabled"]:
                continue
            should, ctx = await rule.trigger(row, cfg)
            if not should:
                continue
            if cfg["mode"] == "autonom":
                await _run_autonomous(rule, row, ctx)
            else:
                await _emit_suggestion(rule, row, ctx)
        except Exception:  # noqa: BLE001
            log.exception("automation %s failed for VIN %s", rule.id, row.vin)
