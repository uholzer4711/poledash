from __future__ import annotations

import logging
from datetime import datetime

from .util import utcnow

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
    skip_email: bool = False,
) -> None:
    """Send a deduped notification via all configured channels.

    Channels: ntfy, email, Telegram. Each is skipped if not configured.
    Dedup ensures each (kind, key) fires at most once."""
    with Session(get_engine()) as session:
        if _already_sent(session, kind, key):
            return

    sent = False
    s = get_settings()

    # --- ntfy ---
    if s.ntfy_topic:
        url = f"{s.ntfy_url.rstrip('/')}/{s.ntfy_topic}"
        headers: dict[str, str] = {"Title": title, "Priority": priority}
        if tags:
            headers["Tags"] = ",".join(tags)
        auth = None
        if s.ntfy_username and s.ntfy_password:
            auth = (s.ntfy_username, s.ntfy_password)
        try:
            with httpx.Client(timeout=10) as client:
                r = client.post(url, content=body.encode("utf-8"), headers=headers, auth=auth)
                r.raise_for_status()
            sent = True
        except Exception:  # noqa: BLE001
            log.exception("ntfy send failed kind=%s key=%s", kind, key)

    # --- Email ---
    if s.smtp_host and s.smtp_to and not skip_email:
        try:
            from .email_notify import email_send
            email_send(f"[poledash] {title}", body)
            sent = True
        except Exception:  # noqa: BLE001
            log.exception("Email send failed kind=%s key=%s", kind, key)

    # --- Telegram ---
    if s.telegram_bot_token and s.telegram_chat_id:
        try:
            from .telegram import telegram_send
            telegram_send(f"<b>{title}</b>\n{body}")
            sent = True
        except Exception:  # noqa: BLE001
            log.exception("Telegram send failed kind=%s key=%s", kind, key)

    if not sent:
        log.debug("No notification channel configured for kind=%s key=%s", kind, key)
        return

    with Session(get_engine()) as session:
        if _already_sent(session, kind, key):
            return
        session.add(
            NotificationLog(sent_at=utcnow(), vin=vin, kind=kind, key=key)
        )
        session.commit()
    log.info("Sent notification kind=%s key=%s", kind, key)
