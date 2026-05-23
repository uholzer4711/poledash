from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ..config import get_settings
from ..metrics import render_metrics

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/metrics")
def metrics() -> Response:
    if get_settings().demo_mode:
        raise HTTPException(status_code=404, detail="disabled in demo")
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)
