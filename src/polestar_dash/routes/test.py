"""Experimental "Test" page — surfaces the new C3 data (GPS, exterior, climate,
air quality, weather, extended health). Read-only, isolated from the poll loop.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..polestar.c3 import get_c3_snapshot
from ..polestar.c3_control import COMMAND_META, run_command
from ..templating import make_templates

templates = make_templates()
router = APIRouter()


@router.get("/test", response_class=HTMLResponse)
def test_page(request: Request):
    return templates.TemplateResponse(request, "test.html", {"commands": COMMAND_META})


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
