"""One-time tokens for deferred C3 commands (automation "suggest" mode).

A rule that fires in *vorschlag* mode doesn't touch the car — it parks the
command here under an unguessable token and pushes a notification linking to
``/confirm/{token}``. Tapping it (through the same reverse-proxy auth as the
rest of the app) atomically consumes the token and runs the command. Short TTL
+ one-time consumption mean a leaked/stale token can't actuate the car: it
expires fast, fires at most once, and the confirm route itself is behind auth.
"""
from __future__ import annotations

import json
import secrets
from datetime import timedelta

from sqlalchemy import update
from sqlmodel import Session

from .db.models import PendingAction
from .db.session import get_engine
from .util import utcnow

DEFAULT_TTL_MIN = 30


def create_pending(
    rule_id: str,
    command: str,
    params: dict | None = None,
    *,
    vin: str | None = None,
    ttl_min: int = DEFAULT_TTL_MIN,
) -> str:
    token = secrets.token_urlsafe(24)
    now = utcnow()
    row = PendingAction(
        token=token,
        created_at=now,
        expires_at=now + timedelta(minutes=ttl_min),
        rule_id=rule_id,
        command=command,
        params_json=json.dumps(params or {}),
        vin=vin,
    )
    with Session(get_engine()) as s:
        s.add(row)
        s.commit()
    return token


def get_pending(token: str) -> PendingAction | None:
    """Fetch a token, lazily flipping an expired-but-still-pending row."""
    with Session(get_engine()) as s:
        row = s.get(PendingAction, token)
        if row is None:
            return None
        if row.status == "pending" and row.expires_at < utcnow():
            row.status = "expired"
            s.add(row)
            s.commit()
            s.refresh(row)
        s.expunge(row)
        return row


def consume_pending(token: str) -> PendingAction | None:
    """Atomically claim a pending token (pending → consumed). Returns the row
    only to the single caller that won the claim; None if missing, expired or
    already handled (guards against a double-tap firing the command twice)."""
    now = utcnow()
    with Session(get_engine()) as s:
        res = s.execute(
            update(PendingAction)
            .where(
                PendingAction.token == token,
                PendingAction.status == "pending",
                PendingAction.expires_at >= now,
            )
            .values(status="consumed", consumed_at=now)
        )
        s.commit()
        if getattr(res, "rowcount", 0) != 1:
            return None
        row = s.get(PendingAction, token)
        if row is not None:
            s.expunge(row)
        return row


def finalize_pending(token: str, result: dict) -> None:
    """Record the command result and flip consumed → done."""
    with Session(get_engine()) as s:
        row = s.get(PendingAction, token)
        if row is None:
            return
        row.status = "done"
        row.result_json = json.dumps(result)[:2000]
        s.add(row)
        s.commit()


def sweep_pending(keep_hours: int = 48) -> int:
    """Delete old rows so the table can't grow unbounded. Returns rows removed."""
    cutoff = utcnow() - timedelta(hours=keep_hours)
    with Session(get_engine()) as s:
        res = s.execute(
            PendingAction.__table__.delete().where(PendingAction.created_at < cutoff)
        )
        s.commit()
    return getattr(res, "rowcount", 0) or 0
