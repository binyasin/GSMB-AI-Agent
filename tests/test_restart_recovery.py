"""Integration-level restart-recovery tests: combines the scheduler's
stale-lock recovery with the queue manager's retry-eligibility rules to
confirm a crash mid-call leads to at most one legitimate retry, never a
silent duplicate dial (spec Sec.15, Sec.17, Sec.44)."""

from __future__ import annotations

import datetime as dt

from app.google_sheets import GoogleSheetRepository
from app.models import CallJob
from app.queue_manager import build_daily_queue
from app.scheduler import recover_stale_locks
from app.schemas import CallState
from tests.conftest import make_sample_worksheet

JOB_DATE = dt.date(2026, 8, 10)


def _seed(session):
    records = GoogleSheetRepository(make_sample_worksheet()).read_rows()
    return build_daily_queue(session, records, job_date=JOB_DATE)


def test_crash_mid_dial_then_restart_allows_exactly_one_retry(db_session):
    _seed(db_session)
    job = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=JOB_DATE).one()

    # Simulate: process acquired the lock, started dialing, then crashed
    # before any webhook/finalize could run.
    crash_time = dt.datetime(2026, 8, 10, 9, 30, tzinfo=dt.timezone.utc)
    job.state = CallState.DIALING.value
    job.attempt_count = 1
    job.locked_at = crash_time
    job.locked_by = "worker-that-died"
    db_session.commit()

    # "Restart" 20 minutes later — recover_stale_locks is what boot calls.
    restart_time = crash_time + dt.timedelta(minutes=20)
    recovered = recover_stale_locks(db_session, restart_time, stale_after_minutes=15)
    assert len(recovered) == 1

    db_session.refresh(job)
    assert job.state == CallState.FAILED.value
    assert job.locked_at is None

    # Rebuilding the queue must make it eligible again (attempt_count=1 < MAX_CALL_RETRIES=3)...
    queue = build_daily_queue(db_session, GoogleSheetRepository(make_sample_worksheet()).read_rows(), job_date=JOB_DATE)
    assert "CN-001" in {c.consumer_no for c in queue}

    # ...but must NOT have created a second CallJob row for the same day (no duplicate).
    jobs_today = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=JOB_DATE).all()
    assert len(jobs_today) == 1


def test_crash_mid_dial_after_retries_exhausted_is_not_requeued(db_session):
    _seed(db_session)
    job = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=JOB_DATE).one()

    crash_time = dt.datetime(2026, 8, 10, 9, 30, tzinfo=dt.timezone.utc)
    job.state = CallState.DIALING.value
    job.attempt_count = 3  # already at MAX_CALL_RETRIES
    job.locked_at = crash_time
    job.locked_by = "worker-that-died"
    db_session.commit()

    restart_time = crash_time + dt.timedelta(minutes=20)
    recover_stale_locks(db_session, restart_time, stale_after_minutes=15)

    queue = build_daily_queue(db_session, GoogleSheetRepository(make_sample_worksheet()).read_rows(), job_date=JOB_DATE)
    assert "CN-001" not in {c.consumer_no for c in queue}


def test_restart_shortly_after_lock_does_not_disturb_a_genuinely_active_call(db_session):
    _seed(db_session)
    job = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=JOB_DATE).one()

    lock_time = dt.datetime(2026, 8, 10, 9, 30, tzinfo=dt.timezone.utc)
    job.state = CallState.IN_PROGRESS.value
    job.locked_at = lock_time
    job.locked_by = "worker-still-alive"
    db_session.commit()

    # A restart happening 2 minutes later (e.g. an unrelated process bounce)
    # must not touch a call that's still plausibly in progress.
    soon_after = lock_time + dt.timedelta(minutes=2)
    recovered = recover_stale_locks(db_session, soon_after, stale_after_minutes=15)
    assert recovered == []

    db_session.refresh(job)
    assert job.state == CallState.IN_PROGRESS.value
    assert job.locked_at is not None
