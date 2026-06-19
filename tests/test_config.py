"""The poll intervals are floored at 2 min (ge=2) — the rate-limit invariant:
never poll the Polestar API faster than ~60s, or throttling shows up as
'not authorized'."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from polestar_dash.config import Settings


def test_poll_interval_enforces_2min_floor():
    with pytest.raises(ValidationError):
        Settings(poll_interval_min=1)
    with pytest.raises(ValidationError):
        Settings(fast_poll_interval_min=1)


def test_poll_interval_accepts_2min():
    s = Settings(poll_interval_min=2, fast_poll_interval_min=2)
    assert s.poll_interval_min == 2
    assert s.fast_poll_interval_min == 2
