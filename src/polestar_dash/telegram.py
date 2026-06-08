"""Telegram bot sender — synchronous blocking send.

Called from `notify()`, which runs off the event loop (in a worker thread), so
this is a plain blocking HTTP call. It raises on failure so the caller's
per-channel dedup only records a row once the message actually went out."""

from __future__ import annotations

import logging

import httpx

from .config import get_settings

log = logging.getLogger(__name__)


def telegram_send(
    text: str, *, button_text: str | None = None, button_url: str | None = None
) -> None:
    s = get_settings()
    if not (s.telegram_bot_token and s.telegram_chat_id):
        return
    url = f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage"
    payload: dict = {
        "chat_id": s.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if button_text and button_url:
        # One inline URL button (not a callback — that would need a bot webhook
        # ingress; a plain URL opens the proxy-authenticated confirm page).
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": button_text, "url": button_url}]]
        }
    with httpx.Client(timeout=10) as client:
        r = client.post(url, json=payload)
        r.raise_for_status()
    log.info("Sent Telegram message")
