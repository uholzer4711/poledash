"""CSRF origin-guard middleware (main.create_app).

The reverse proxy's basic-auth is ambient browser state, so a foreign page
could fire our query-param POSTs. The guard blocks unsafe methods whose
Sec-Fetch-Site / Origin headers mark them as cross-site; header-less clients
(curl, ntfy action buttons) stay unaffected.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from polestar_dash.main import create_app

URL = "/api/app-settings/language?value=de"


def _client() -> TestClient:
    # No `with`: the lifespan (Polestar login, scheduler) must not run.
    return TestClient(create_app())


def test_cross_site_fetch_blocked(tmp_db):
    r = _client().post(URL, headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403


def test_same_site_subdomain_blocked(tmp_db):
    r = _client().post(URL, headers={"sec-fetch-site": "same-site"})
    assert r.status_code == 403


def test_same_origin_allowed(tmp_db):
    r = _client().post(URL, headers={"sec-fetch-site": "same-origin"})
    assert r.status_code == 200


def test_foreign_origin_blocked(tmp_db):
    r = _client().post(URL, headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_sandboxed_null_origin_blocked(tmp_db):
    r = _client().post(URL, headers={"origin": "null"})
    assert r.status_code == 403


def test_matching_origin_allowed(tmp_db):
    # TestClient targets http://testserver with Host: testserver.
    r = _client().post(URL, headers={"origin": "http://testserver"})
    assert r.status_code == 200


def test_headerless_client_allowed(tmp_db):
    r = _client().post(URL)
    assert r.status_code == 200


def test_get_never_blocked(tmp_db):
    r = _client().get("/health", headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 200
