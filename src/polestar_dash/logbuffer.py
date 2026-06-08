"""In-memory ring buffer of the app's own log records, surfaced on /log.

A ``RingBufferHandler`` attached to the ``polestar_dash`` logger keeps the last
N records in a bounded deque, so the /log page can show "what the tool is doing"
without a DB table to grow or migrate. Durable history still lives in
``docker compose logs``; this is the live activity feed.

Only the app's own loggers are captured (``polestar_dash.*``) — not uvicorn
access logs or httpx chatter — so the feed reads as a meaningful activity log:
polls, persistence, sessions, notifications, automations, errors.
"""
from __future__ import annotations

import logging
from collections import deque
from threading import Lock

_MAX = 2000
_buf: deque[dict] = deque(maxlen=_MAX)
_lock = Lock()
_seq = 0


class RingBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        global _seq
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 — a bad format string must not crash logging
            return
        exc = None
        if record.exc_info:
            try:
                exc = logging.Formatter().formatException(record.exc_info)
            except Exception:  # noqa: BLE001
                exc = None
        with _lock:
            _seq += 1
            _buf.append({
                "id": _seq,
                "ts": record.created,        # epoch seconds (float)
                "level": record.levelname,
                "levelno": record.levelno,
                "logger": record.name,
                "msg": msg,
                "exc": exc,
            })


def get_records(*, limit: int = 300, min_level: int = 0, since_id: int = 0) -> list[dict]:
    """Most recent records matching the filters, oldest-first, capped to `limit`."""
    with _lock:
        items = list(_buf)
    out = [r for r in items if r["levelno"] >= min_level and r["id"] > since_id]
    return out[-limit:]


def install(logger_name: str = "polestar_dash", level: int = logging.INFO) -> None:
    """Attach the ring-buffer handler to the app logger (idempotent)."""
    lg = logging.getLogger(logger_name)
    if any(isinstance(h, RingBufferHandler) for h in lg.handlers):
        return
    handler = RingBufferHandler()
    handler.setLevel(level)
    lg.addHandler(handler)
