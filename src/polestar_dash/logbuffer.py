"""In-memory ring buffer of the app's own log records, surfaced on /log.

A ``RingBufferHandler`` attached to the ``polestar_dash`` logger keeps the last
N records in a bounded deque — the live activity feed shown on /log. The same
handler also appends to a ``_pending`` queue that the scheduler drains into the
``LogEntry`` table (see ``scheduler._persist_logs``), giving durable history
that survives restarts and reaches beyond the ring's ``maxlen``; the /log tab
pages back through it via ``/api/log/history``. ``docker compose logs`` remains
the lowest-level fallback.

Only the app's own loggers are captured (``polestar_dash.*``) — not uvicorn
access logs or httpx chatter — so the feed reads as a meaningful activity log:
polls, persistence, sessions, notifications, automations, errors.
"""
from __future__ import annotations

import logging
from collections import deque
from threading import Lock

_MAX = 2000
# Separate, larger holding area drained to the DB by the scheduler. The ring
# above is the live tail (capped, may drop old lines between polls); _pending
# accumulates *every* record until the next flush so history is never lost to
# the ring's maxlen. Capped well above any realistic per-flush volume so a
# stuck flusher can't grow it without bound.
_PENDING_MAX = 100_000
_buf: deque[dict] = deque(maxlen=_MAX)
_pending: deque[dict] = deque(maxlen=_PENDING_MAX)
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
            rec = {
                "id": _seq,
                "ts": record.created,        # epoch seconds (float)
                "level": record.levelname,
                "levelno": record.levelno,
                "logger": record.name,
                "msg": msg,
                "exc": exc,
            }
            _buf.append(rec)
            _pending.append(rec)


def drain_pending() -> list[dict]:
    """Atomically remove and return all records awaiting DB persistence.

    Called by the scheduler's flush job. On a failed write the caller is
    responsible for re-queueing (see ``requeue_pending``)."""
    with _lock:
        out = list(_pending)
        _pending.clear()
    return out


def requeue_pending(records: list[dict]) -> None:
    """Put records back at the front of the pending queue after a failed flush
    so the next flush retries them (oldest-first preserved)."""
    with _lock:
        _pending.extendleft(reversed(records))


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
