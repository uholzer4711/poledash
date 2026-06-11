from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from ..db.models import C3Snapshot, VehicleSnapshot
from ..db.session import get_engine
from ..templating import make_templates
from ..util import utcnow

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


def _fresh_c3(vin: str | None) -> C3Snapshot | None:
    """Latest persisted C3 row, but only when recent enough to trust its
    charge-time horizons (the C3 loop fast-polls every ~2 min while charging,
    so anything older than ~10 min predates the current charge state). Used by
    the charging card to show the car's own time-to-target instead of the
    static interpolation. Never breaks the dashboard."""
    try:
        with Session(get_engine()) as s:
            stmt = select(C3Snapshot).order_by(C3Snapshot.fetched_at.desc())
            if vin:
                stmt = stmt.where(C3Snapshot.vin == vin)
            row = s.exec(stmt.limit(1)).first()
        if row is not None and row.fetched_at >= utcnow() - timedelta(minutes=10):
            return row
    except Exception:  # noqa: BLE001
        log.exception("C3 lookup for charging card failed")
    return None


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, vin: str | None = None):
    snap = _latest_for_vin(vin)
    return templates.TemplateResponse(
        request, "dashboard.html", {"s": snap, "soh": _soh(), "c3": _fresh_c3(vin)}
    )


@router.get("/partials/cards", response_class=HTMLResponse)
def cards(request: Request, vin: str | None = None):
    snap = _latest_for_vin(vin)
    return templates.TemplateResponse(
        request, "partials/cards.html", {"s": snap, "c3": _fresh_c3(vin)}
    )
