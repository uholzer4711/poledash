"""Experimental "Test" page — surfaces the new C3 data (GPS, exterior, climate,
air quality, weather, extended health). Read-only, isolated from the poll loop.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from ..polestar.c3 import get_c3_snapshot
from ..polestar.c3_control import run_command

router = APIRouter()


@router.get("/test")
def test_page():
    # The live C3 view + control panel now live on the consolidated Lab tab.
    # Keep the old URL working via redirect.
    return RedirectResponse(url="/lab#frame-live", status_code=307)


@router.get("/api/test/c3")
async def c3_data(force: bool = False) -> dict:
    """Live, decoded snapshot of the C3 backend (cached ~30 s)."""
    return await get_c3_snapshot(force=force)


class ControlRequest(BaseModel):
    command: str
    params: dict | None = None


@router.post("/api/test/control")
async def c3_control(req: ControlRequest) -> dict:
    """Fire one C3 write command (lock, climate, …). Real side effect on the car."""
    return await run_command(req.command, req.params)
