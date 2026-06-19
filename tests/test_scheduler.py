"""_reschedule / _reschedule_c3 run from poll_once's `finally`, so they must
never raise — a failure there used to break the single-shot trigger chain and
stall the poll loop until restart (the v1.40.0 robustness fix)."""

from __future__ import annotations

from polestar_dash import scheduler


class _FakeSched:
    """Records add_job calls; raises on the first `fail_times` of them."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls: list = []

    def add_job(self, *args, **kwargs):
        self.calls.append(kwargs.get("id"))
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("scheduler rejected job")


def test_reschedule_falls_back_when_first_add_job_fails(tmp_db, monkeypatch):
    fake = _FakeSched(fail_times=1)
    monkeypatch.setattr(scheduler, "_SCHEDULER", fake)
    scheduler._reschedule(object(), charging=False)   # must not raise
    assert len(fake.calls) == 2  # primary failed, bare fallback job armed


def test_reschedule_never_raises_when_both_add_jobs_fail(tmp_db, monkeypatch):
    fake = _FakeSched(fail_times=99)
    monkeypatch.setattr(scheduler, "_SCHEDULER", fake)
    scheduler._reschedule(object(), charging=True)    # must not raise
    assert len(fake.calls) == 2  # tried primary + fallback, then gave up quietly


def test_reschedule_survives_interval_calc_failure(tmp_db, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("interval calc blew up")

    fake = _FakeSched(fail_times=0)
    monkeypatch.setattr(scheduler, "_SCHEDULER", fake)
    monkeypatch.setattr(scheduler, "_adaptive_interval", _boom)
    scheduler._reschedule(object(), charging=False)   # must not raise
    assert len(fake.calls) == 1  # armed at the safe fallback interval


def test_reschedule_c3_never_raises_when_add_job_fails(tmp_db, monkeypatch):
    fake = _FakeSched(fail_times=99)
    monkeypatch.setattr(scheduler, "_SCHEDULER", fake)
    scheduler._reschedule_c3(charging=False)          # must not raise
    assert len(fake.calls) == 2
