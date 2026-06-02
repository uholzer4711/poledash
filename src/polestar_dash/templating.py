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

from . import __version__
from .i18n import get_language, t
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
    tpl = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    tpl.env.filters["localtime"] = _localtime
    tpl.env.filters["address_label"] = label_for
    tpl.env.globals["demo_mode"] = s.demo_mode
    tpl.env.globals["demo_banner_text"] = s.demo_banner_text
    tpl.env.globals["app_version"] = __version__
    tpl.env.globals["c3_enabled"] = s.c3_enabled

    def car_image_src() -> str:
        """Vehicle image to show: an uploaded file (data dir) wins, else the
        configured CAR_IMAGE_URL, else empty (→ built-in SVG). Checked per
        render so an upload takes effect without a restart."""
        p = s.data_dir / "car_image"
        try:
            if p.exists():
                return f"/api/car-image?v={int(p.stat().st_mtime)}"
        except OSError:
            pass
        return s.car_image_url or ""

    tpl.env.globals["car_image_src"] = car_image_src
    # i18n: `t()` for strings, `ui_lang` for <html lang> + the settings toggle.
    # Globals are evaluated per render, and t() reads the live language cache,
    # so a language switch takes effect on the next page load without a rebuild.
    tpl.env.globals["t"] = t
    tpl.env.globals["ui_lang"] = get_language
    return tpl
