from __future__ import annotations

from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from polestar_dash.db.models import VehicleSnapshot
from polestar_dash.db.session import get_engine
from polestar_dash.routes import api as api_routes
from polestar_dash.routes import dashboard, health, raw
from polestar_dash.util import utcnow


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
    now = utcnow()
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

    now = utcnow()
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


def test_lab_probe_rejects_graphql_injection(tmp_db):
    from polestar_dash.routes import lab

    app = FastAPI()
    app.include_router(lab.router)
    app.state.client = None  # validation must happen before this is dereferenced
    client = TestClient(app)

    # Injection attempts must be rejected up-front with 400 (before any client
    # work), so a malicious field never reaches template.format().
    for bad in ["name) { x } evil(", 'a"b', "name @skip", "x: y", "name(a:1)"]:
        r = client.post("/api/lab/probe", json={"parent": "BatteryV2", "fields": [bad]})
        assert r.status_code == 400, (bad, r.status_code)

    # A legitimate field shape passes validation (then 503 — no client wired in
    # this bare test app — which proves it got past the allowlist).
    r = client.post("/api/lab/probe", json={"parent": "BatteryV2", "fields": ["name { a b }"]})
    assert r.status_code == 503


def test_soh_estimate(tmp_db):
    from polestar_dash.db.models import ChargeSession
    from polestar_dash.insights_extras import compute_soh

    # Too little data → no estimate, "collecting".
    assert compute_soh()["enough_data"] is False

    now = utcnow()
    with Session(get_engine()) as s:
        for i in range(5):
            # ~75 kWh implied (30 kWh over a 40% SoC span) — a healthy pack.
            s.add(ChargeSession(
                vin="VIN1", started_at=now - timedelta(days=i),
                soc_start=30, soc_end=70, energy_kwh=30.0, is_complete=True,
            ))
        s.commit()

    soh = compute_soh()
    assert soh["enough_data"] is True
    assert soh["samples"] >= 3
    assert soh["soh_pct"] is not None and 0 < soh["soh_pct"] <= 100  # never > 100
    assert soh["implied_kwh"] == 75.0
