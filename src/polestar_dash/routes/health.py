import contextlib

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from sqlalchemy import func
from sqlmodel import Session, select

from .. import __version__
from ..config import get_settings
from ..db.models import LogEntry, VehicleSnapshot
from ..db.session import get_engine
from ..logbuffer import pending_size
from ..metrics import render_metrics
from ..util import utcnow

router = APIRouter()


@router.get("/health")
def health() -> dict:
    """Liveness + lightweight observability. Always 200 so the container
    healthcheck stays green; the body carries operational detail (last poll
    age, pending log-flush backlog, DB size) for an external monitor."""
    s = get_settings()
    out: dict = {"status": "ok", "version": __version__}

    try:
        with Session(get_engine()) as sess:
            last = sess.exec(
                select(VehicleSnapshot.fetched_at)
                .order_by(VehicleSnapshot.fetched_at.desc())
                .limit(1)
            ).first()
            out["log_rows"] = sess.exec(select(func.count()).select_from(LogEntry)).one()
        if last is not None:
            out["last_poll_at"] = last.isoformat()
            out["last_poll_age_s"] = round((utcnow() - last).total_seconds())
    except Exception as e:  # noqa: BLE001 — health must never 500
        out["db_error"] = str(e)

    out["log_pending"] = pending_size()
    with contextlib.suppress(OSError):
        out["db_bytes"] = s.db_path.stat().st_size
    return out


@router.get("/metrics")
def metrics() -> Response:
    if get_settings().demo_mode:
        raise HTTPException(status_code=404, detail="disabled in demo")
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)
