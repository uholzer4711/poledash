"""Telegram bot sender — fire-and-forget message sending."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

import httpx

from .config import get_settings

log = logging.getLogger(__name__)


async def _send(text: str) -> None:
    s = get_settings()
    if not (s.telegram_bot_token and s.telegram_chat_id):
        return
    url = f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                url,
                json={
                    "chat_id": s.telegram_chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            r.raise_for_status()
        log.info("Sent Telegram message")
    except Exception:  # noqa: BLE001
        log.exception("Telegram send failed")


def telegram_send(text: str) -> None:
    """Schedule a telegram send from sync/scheduler context."""
    s = get_settings()
    if not (s.telegram_bot_token and s.telegram_chat_id):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("telegram_send called outside an event loop; dropping")
        return
    task = loop.create_task(_send(text))
    with suppress(Exception):
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
