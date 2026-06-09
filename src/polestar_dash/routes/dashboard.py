from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from ..db.models import VehicleSnapshot
from ..db.session import get_engine
from ..templating import make_templates

router = APIRouter()
templates = make_templates()
log = logging.getLogger(__name__)


def _soh() -> dict | None:
    """Battery State-of-Health estimate for the dashboard. Computed at page load
    (not on the 30 s HTMX card refresh) since it scans charge + trip history and
    barely changes. Never break the dashboard if it fails."""
    try:
        from ..insights_extras import compute_soh

        return compute_soh()
    except Exception:  # noqa: BLE001
        log.exception("SoH computation failed")
        return None


def _latest_for_vin(vin: str | None) -> VehicleSnapshot | None:
    with Session(get_engine()) as s:
        stmt = select(VehicleSnapshot).order_by(VehicleSnapshot.fetched_at.desc())
        if vin:
            stmt = stmt.where(VehicleSnapshot.vin == vin)
        return s.exec(stmt.limit(1)).first()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, vin: str | None = None):
    snap = _latest_for_vin(vin)
    return templates.TemplateResponse(request, "dashboard.html", {"s": snap, "soh": _soh()})


@router.get("/partials/cards", response_class=HTMLResponse)
def cards(request: Request, vin: str | None = None):
    snap = _latest_for_vin(vin)
    return templates.TemplateResponse(request, "partials/cards.html", {"s": snap})
