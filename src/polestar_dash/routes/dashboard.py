from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from ..db.models import VehicleSnapshot
from ..db.session import get_engine
from ..templating import make_templates

router = APIRouter()
templates = make_templates()


def _latest_for_vin(vin: str | None) -> VehicleSnapshot | None:
    with Session(get_engine()) as s:
        stmt = select(VehicleSnapshot).order_by(VehicleSnapshot.fetched_at.desc())
        if vin:
            stmt = stmt.where(VehicleSnapshot.vin == vin)
        return s.exec(stmt.limit(1)).first()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, vin: str | None = None):
    snap = _latest_for_vin(vin)
    return templates.TemplateResponse(request, "dashboard.html", {"s": snap})


@router.get("/partials/cards", response_class=HTMLResponse)
def cards(request: Request, vin: str | None = None):
    snap = _latest_for_vin(vin)
    return templates.TemplateResponse(request, "partials/cards.html", {"s": snap})
