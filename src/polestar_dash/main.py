from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .db.session import init_engine
from .polestar.client import PolestarClient
from .routes import api as api_routes
from .routes import dashboard, health, lab, pages, raw
from .routes import test as test_routes
from .scheduler import build_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("polestar_dash")

# Capture the app's own log stream into an in-memory ring buffer for the /log tab.
from .logbuffer import install as _install_logbuffer  # noqa: E402

_install_logbuffer()

PKG_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_engine(settings.db_path)

    client = PolestarClient(
        email=settings.polestar_email,
        password=settings.polestar_password,
        vins=settings.vin_list,
    )
    try:
        await client.connect()
    except Exception:
        log.exception("Polestar login failed at startup — scheduler will retry on its interval")

    sched = build_scheduler(client, settings.poll_interval_min)
    sched.start()
    log.info("Scheduler started (every %d min)", settings.poll_interval_min)

    app.state.client = client
    app.state.scheduler = sched
    try:
        yield
    finally:
        sched.shutdown(wait=False)
        # Persist any log lines still in the ring so a restart loses nothing.
        try:
            from .scheduler import flush_logs_now

            flush_logs_now()
        except Exception:
            log.exception("final log flush on shutdown failed")


def create_app() -> FastAPI:
    app = FastAPI(title="polestar-dash", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(api_routes.router)
    app.include_router(dashboard.router)
    app.include_router(raw.router)
    app.include_router(pages.router)
    app.include_router(lab.router)
    app.include_router(test_routes.router)

    app.mount("/static", StaticFiles(directory=str(PKG_DIR / "static")), name="static")
    return app


app = create_app()


def run() -> None:
    s = get_settings()
    uvicorn.run(
        "polestar_dash.main:app",
        host=s.host,
        port=s.port,
        log_level="info",
    )


if __name__ == "__main__":
    run()
