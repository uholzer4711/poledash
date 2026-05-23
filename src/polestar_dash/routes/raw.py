from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlmodel import Session, select

from ..db.models import RawSnapshot
from ..db.session import get_engine
from ..templating import make_templates

router = APIRouter()
templates = make_templates()


def _latest(vin: str | None) -> RawSnapshot | None:
    with Session(get_engine()) as s:
        stmt = select(RawSnapshot).order_by(RawSnapshot.fetched_at.desc())
        if vin:
            stmt = stmt.where(RawSnapshot.vin == vin)
        return s.exec(stmt.limit(1)).first()


@router.get("/raw.json")
def raw_json(vin: str | None = None):
    row = _latest(vin)
    if row is None:
        raise HTTPException(404, "No data yet")
    return JSONResponse(
        {"vin": row.vin, "fetched_at": row.fetched_at.isoformat(), "payload": row.payload}
    )


@router.get("/raw", response_class=HTMLResponse)
def raw_html(request: Request, vin: str | None = None):
    row = _latest(vin)
    pretty = (
        json.dumps(row.payload, indent=2, ensure_ascii=False, default=str) if row else None
    )
    return templates.TemplateResponse(
        request,
        "raw.html",
        {"row": row, "pretty": pretty},
    )
