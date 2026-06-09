from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from sqlmodel import Session, select

from ..db.models import RawSnapshot
from ..db.session import get_engine

router = APIRouter()


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


@router.get("/raw")
def raw_html():
    # The raw payload now lives in a collapsible frame on the consolidated Lab
    # tab (it lazy-loads /raw.json). Keep the old URL working via redirect.
    return RedirectResponse(url="/lab#frame-raw", status_code=307)
