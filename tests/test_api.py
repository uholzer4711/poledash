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
    body = client.get("/health").json()
    # Liveness contract: always 200 + status ok; extra observability fields
    # (version, log_pending, …) may be present.
    assert body["status"] == "ok"
    assert "version" in body
    assert body["log_pending"] >= 0


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


def test_log_history_filter_and_export(tmp_db):
    from polestar_dash.db.models import LogEntry

    now = datetime.utcnow()
    rows = [
        ("polestar_dash.scheduler", "INFO", 20, "Stored snapshot for VIN X", 0),
        ("polestar_dash.scheduler", "WARNING", 30, "slow fetch", 1),
        ("polestar_dash.notify", "ERROR", 40, "ntfy send failed", 2),
    ]
    with Session(get_engine()) as s:
        for logger, level, levelno, msg, age in rows:
            s.add(LogEntry(ts=now - timedelta(minutes=age), level=level,
                           levelno=levelno, logger=logger, msg=msg))
        s.commit()

    client = TestClient(_app())

    # Level floor: ERROR returns only the error row.
    recs = client.get("/api/log/history?level=ERROR").json()["records"]
    assert [r["level"] for r in recs] == ["ERROR"]

    # Text search on the message.
    recs = client.get("/api/log/history?level=DEBUG&q=snapshot").json()["records"]
    assert len(recs) == 1 and "snapshot" in recs[0]["msg"]

    # Exact logger filter.
    recs = client.get("/api/log/history?level=DEBUG&logger=polestar_dash.notify").json()["records"]
    assert len(recs) == 1 and recs[0]["logger"] == "polestar_dash.notify"

    # Oldest-first ordering for the client store.
    recs = client.get("/api/log/history?level=DEBUG").json()["records"]
    assert [r["ts"] for r in recs] == sorted(r["ts"] for r in recs)

    # Distinct loggers, short labels.
    labels = {o["label"] for o in client.get("/api/log/loggers").json()["loggers"]}
    assert {"scheduler", "notify"} <= labels

    # Plain-text export, chronological, attachment.
    resp = client.get("/api/log/export?level=DEBUG")
    assert "attachment" in resp.headers["content-disposition"]
    assert "Stored snapshot" in resp.text
