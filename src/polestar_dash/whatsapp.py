"""WhatsApp sender via a self-hosted WAHA (WhatsApp HTTP API) sidecar.

Called from `notify()`, which runs off the event loop (in a worker thread), so
this is a plain blocking HTTP call. It raises on failure so the caller's
per-channel dedup only records a row once the message actually went out.

Not an official WhatsApp API — the sidecar (github.com/devlikeapro/waha,
`devlikeapro/waha` Docker image, see docker-compose.yml "whatsapp" profile)
runs the GOWS engine (Go/whatsmeow, plain WebSocket protocol — no browser/
Puppeteer), specifically to sidestep the "stale user-agent" breakage the
Puppeteer-based engines (WAHA's own WEBJS default, and open-wa/wa-automate,
which was tried first — see CHANGELOG v1.48.0) were hitting against
WhatsApp's web client in 2026-07. Verified against the real
devlikeapro/waha source (`src/api/chatting.controller.ts`,
`src/structures/chatting.dto.ts`), not against third-party docs.
"""

from __future__ import annotations

import logging

import httpx

from .config import get_settings

log = logging.getLogger(__name__)

# WAHA Core runs a single session; multi-session (Plus) isn't used here.
_SESSION = "default"


def whatsapp_send(text: str) -> None:
    s = get_settings()
    if not (s.whatsapp_url and s.whatsapp_api_key and s.whatsapp_to):
        return
    url = f"{s.whatsapp_url.rstrip('/')}/api/sendText"
    headers = {"X-Api-Key": s.whatsapp_api_key}
    payload = {"session": _SESSION, "chatId": s.whatsapp_to, "text": text}
    with httpx.Client(timeout=10) as client:
        r = client.post(url, json=payload, headers=headers)
        r.raise_for_status()
    log.info("Sent WhatsApp message")
