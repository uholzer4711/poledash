from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("POLESTAR_EMAIL", "test@example.com")
os.environ.setdefault("POLESTAR_PASSWORD", "test")


@pytest.fixture()
def tmp_db(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    monkeypatch.setenv("DATA_DIR", str(tmp))
    # reset cached settings
    from polestar_dash import config as cfg

    cfg._settings = None
    from polestar_dash.db import session as dbs

    dbs._engine = None
    dbs.init_engine(tmp / "test.sqlite")
    yield tmp
