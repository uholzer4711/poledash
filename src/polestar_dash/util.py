"""Tiny helpers shared across the package — no internal imports allowed."""

from __future__ import annotations

import math
from datetime import UTC, datetime


def utcnow() -> datetime:
    """Naive UTC datetime, replacing the deprecated ``datetime.utcnow()``."""
    return datetime.now(UTC).replace(tzinfo=None)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km between two lat/lng points."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
