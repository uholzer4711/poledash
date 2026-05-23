"""Shared Jinja2Templates factory.

Times in the DB are naive UTC. Convert to the user's local zone for
display so a clock reading like "08:51" matches what the dashboard
viewer would say if asked the time, instead of being a UTC value the
reader has to mentally offset.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from .journey_import import label_for

DISPLAY_TZ = ZoneInfo("Europe/Vienna")
_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _localtime(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(DISPLAY_TZ).strftime(fmt)


def make_templates() -> Jinja2Templates:
    from .config import get_settings

    s = get_settings()
    t = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    t.env.filters["localtime"] = _localtime
    t.env.filters["address_label"] = label_for
    t.env.globals["demo_mode"] = s.demo_mode
    t.env.globals["demo_banner_text"] = s.demo_banner_text
    return t
