from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from polestar_dash.db.models import VehicleSnapshot
from polestar_dash.db.session import get_engine
from polestar_dash.routes import api as api_routes
from polestar_dash.routes import dashboard, health, raw


def _app():
    app = FastAPI()
    app.include_router(health.router)
    app.include_router(api_routes.router)
    app.include_router(dashboard.router)
    app.include_router(raw.router)
    return app


def test_health(tmp_db):
    client = TestClient(_app())
    assert client.get("/health").json() == {"status": "ok"}


def test_latest_404_when_empty(tmp_db):
    client = TestClient(_app())
    assert client.get("/api/latest").status_code == 404


def test_latest_and_history(tmp_db):
    now = datetime.utcnow()
    with Session(get_engine()) as s:
        for i in range(3):
            s.add(
                VehicleSnapshot(
                    fetched_at=now - timedelta(hours=i),
                    vin="VIN1",
                    soc_percent=80 - i,
                    range_km=300 - i * 5,
                )
            )
        s.commit()

    client = TestClient(_app())
    latest = client.get("/api/latest").json()
    assert latest["soc_percent"] == 80
    assert latest["vin"] == "VIN1"

    hist = client.get("/api/history?field=soc_percent&days=1").json()
    assert hist["field"] == "soc_percent"
    assert len(hist["points"]) == 3

    bad = client.get("/api/history?field=secret_field&days=1")
    assert bad.status_code == 400
