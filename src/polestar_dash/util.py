"""Tiny helpers shared across the package — no internal imports allowed."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Naive UTC datetime, replacing the deprecated ``datetime.utcnow()``."""
    return datetime.now(UTC).replace(tzinfo=None)
