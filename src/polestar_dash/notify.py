from __future__ import annotations

import logging
from datetime import datetime

import httpx
from sqlmodel import Session, select

from .config import get_settings
from .db.models import NotificationLog
from .db.session import get_engine

log = logging.getLogger(__name__)


def _already_sent(session: Session, kind: str, key: str) -> bool:
    stmt = select(NotificationLog).where(
        NotificationLog.kind == kind, NotificationLog.key == key
    )
    return session.exec(stmt).first() is not None


def notify(
    kind: str,
    key: str,
    title: str,
    body: str,
    *,
    vin: str | None = None,
    priority: str = "default",
    tags: list[str] | None = None,
) -> None:
    """Send a deduped ntfy notification. No-op if NTFY_TOPIC not configured."""
    s = get_settings()
    if not s.ntfy_topic:
        return

    # Dedup *check* up front; the matching dedup *write* only happens after
    # the HTTP POST succeeds. If we burned the key on a failed send the user
    # would never get a retry — the next tick would skip it as "already sent".
    with Session(get_engine()) as session:
        if _already_sent(session, kind, key):
            return

    url = f"{s.ntfy_url.rstrip('/')}/{s.ntfy_topic}"
    headers = {
        "Title": title,
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = ",".join(tags)

    auth = None
    if s.ntfy_username and s.ntfy_password:
        auth = (s.ntfy_username, s.ntfy_password)

    try:
        with httpx.Client(timeout=10) as client:
            r = client.post(url, content=body.encode("utf-8"), headers=headers, auth=auth)
            r.raise_for_status()
    except Exception:  # noqa: BLE001
        log.exception("Failed to send ntfy notification kind=%s key=%s", kind, key)
        return

    with Session(get_engine()) as session:
        # Re-check inside the same tx in case a concurrent caller raced us
        # past the first guard — at worst we send twice, never zero times.
        if _already_sent(session, kind, key):
            return
        session.add(
            NotificationLog(sent_at=datetime.utcnow(), vin=vin, kind=kind, key=key)
        )
        session.commit()
    log.info("Sent ntfy notification kind=%s key=%s", kind, key)
