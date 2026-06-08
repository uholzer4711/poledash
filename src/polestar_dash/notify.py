from __future__ import annotations

import logging

import httpx
from sqlmodel import Session, select

from .config import get_settings
from .db.models import NotificationLog
from .db.session import get_engine
from .util import utcnow

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
    email_html: str | None = None,
    action_url: str | None = None,
    action_label: str | None = None,
) -> None:
    """Send a deduped notification via all configured channels.

    Channels: ntfy, email, Telegram. Each is skipped if not configured.

    Dedup is per channel: each channel records its own `(kind, key#channel)`
    row only after a successful send, so a transient failure on one channel
    (e.g. SMTP blip) no longer marks the whole event as delivered — the others
    still fire, and the failed one backfills on the next call for the same
    event. All sends are synchronous/blocking; callers on the event loop must
    offload this to a worker thread.

    `email_html`, when set, is sent as the HTML email body instead of `body`.
    """
    s = get_settings()

    def channel(name: str, fn) -> None:  # noqa: ANN001
        ckey = f"{key}#{name}"
        with Session(get_engine()) as session:
            # Honour both the per-channel key and the pre-split combined key so
            # events already delivered before this change don't re-fire once.
            if _already_sent(session, kind, ckey) or _already_sent(session, kind, key):
                return
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("%s send failed kind=%s key=%s", name, kind, key)
            return
        with Session(get_engine()) as session:
            if _already_sent(session, kind, ckey):
                return
            session.add(NotificationLog(sent_at=utcnow(), vin=vin, kind=kind, key=ckey))
            session.commit()

    has_action = bool(action_url and action_label)
    # Channels without native action buttons (email) get the link inline.
    email_text = body + (f"\n\n{action_label}: {action_url}" if has_action else "")

    if s.ntfy_topic:
        channel("ntfy", lambda: _send_ntfy(
            s, title, body, priority, tags, action_url, action_label))
    if s.smtp_host and s.smtp_to and not skip_email:
        from .email_notify import email_send
        if email_html is not None:
            channel(
                "email",
                lambda: email_send(
                    f"[poledash] {title}", email_html, html=True, text_alt=email_text
                ),
            )
        else:
            channel("email", lambda: email_send(f"[poledash] {title}", email_text))
    if s.telegram_bot_token and s.telegram_chat_id:
        from .telegram import telegram_send
        btn_text = action_label if has_action else None
        btn_url = action_url if has_action else None
        channel("telegram", lambda: telegram_send(
            f"<b>{title}</b>\n{body}", button_text=btn_text, button_url=btn_url))


def _send_ntfy(  # noqa: ANN001
    s,
    title: str,
    body: str,
    priority: str,
    tags: list[str] | None,
    action_url: str | None = None,
    action_label: str | None = None,
) -> None:
    url = f"{s.ntfy_url.rstrip('/')}/{s.ntfy_topic}"
    headers: dict[str, str] = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = ",".join(tags)
    if action_url and action_label:
        # A "view" action opens the URL in the user's browser, which is already
        # authenticated against the reverse proxy — no creds in the payload.
        # Commas delimit ntfy action fields, so strip them from the label.
        safe = action_label.replace(",", " ")
        headers["Actions"] = f"view, {safe}, {action_url}, clear=true"
    auth = (s.ntfy_username, s.ntfy_password) if (s.ntfy_username and s.ntfy_password) else None
    with httpx.Client(timeout=10) as client:
        r = client.post(url, content=body.encode("utf-8"), headers=headers, auth=auth)
        r.raise_for_status()
    log.info("Sent ntfy notification: %s", title)
