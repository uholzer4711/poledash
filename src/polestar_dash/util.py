"""Tiny helpers shared across the package — no internal imports allowed."""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Naive UTC datetime, replacing the deprecated ``datetime.utcnow()``."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
