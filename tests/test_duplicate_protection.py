from __future__ import annotations

import datetime as dt

import pytest

from app.calling_agent import (
    acquire_job_lock,
    finalize_call_attempt,
    process_next_consumer,
    retry_pending_sheet_syncs,
)
from app.google_sheets import GoogleSheetRepository
from app.models import CallAttempt, CallJob, Consumer, DoNotCall
from app.queue_manager import build_daily_queue
from app.schemas import CallDecision, CallState, CustomerIntent
from tests.conftest import FakeWorksheet, make_sample_worksheet

JOB_DATE = dt.date(2026, 8, 10)


def _seed_queue(session):
    records = GoogleSheetRepository(make_sample_worksheet()).read_rows()
    return build_daily_queue(session, records, job_date=JOB_DATE)


def test_acquire_job_lock_is_atomic_compare_and_set(db_session):
    _seed_queue(db_session)
    job = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=JOB_DATE).one()

    assert acquire_job_lock(db_session, job) is True
    # A second worker attempting the same job must fail to acquire it.
    assert acquire_job_lock(db_session, job) is False


def test_process_next_consumer_dry_run_completes_full_pipeline(db_session, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("TEST_MODE", "true")
    from app.config import get_settings

    get_settings.cache_clear()

    _seed_queue(db_session)
    attempt = process_next_consumer(db_session, job_date=JOB_DATE)

    assert attempt is not None
    assert attempt.result == CallState.COMPLETED.value
    assert attempt.consumer_no == "CN-001"

    job = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=JOB_DATE).one()
    assert job.state == CallState.COMPLETED.value
    assert job.locked_at is None  # released after completion

    consumer = db_session.query(Consumer).filter_by(consumer_no="CN-001").one()
    assert consumer.call_status == CallState.COMPLETED.value
    assert consumer.call_attempt == 1
    assert consumer.transcript is not None


def test_process_next_consumer_returns_none_when_queue_empty(db_session, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    consumer = process_next_consumer(db_session, job_date=JOB_DATE)
    assert consumer is None


def test_finalize_call_attempt_records_do_not_call_registry(db_session):
    _seed_queue(db_session)
    job = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=JOB_DATE).one()
    acquire_job_lock(db_session, job)
    from app.calling_agent import create_attempt

    consumer = db_session.query(Consumer).filter_by(consumer_no="CN-001").one()
    attempt = create_attempt(db_session, job, consumer)

    decision = CallDecision(intent=CustomerIntent.DO_NOT_CALL, do_not_call=True)
    finalize_call_attempt(db_session, attempt, decision, "[]", 12, CallState.DO_NOT_CALL)

    dnc = db_session.query(DoNotCall).filter_by(consumer_no="CN-001").one()
    assert dnc.mobile_number == consumer.mobile_number
    db_session.refresh(consumer)
    assert consumer.do_not_call is True


def test_finalize_call_attempt_marks_already_paid_claim(db_session):
    _seed_queue(db_session)
    job = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=JOB_DATE).one()
    acquire_job_lock(db_session, job)
    from app.calling_agent import create_attempt

    consumer = db_session.query(Consumer).filter_by(consumer_no="CN-001").one()
    attempt = create_attempt(db_session, job, consumer)

    decision = CallDecision(intent=CustomerIntent.ALREADY_PAID, human_followup=True)
    finalize_call_attempt(db_session, attempt, decision, "[]", 30, CallState.COMPLETED)

    db_session.refresh(consumer)
    assert consumer.already_paid == "CUSTOMER_CLAIMS_PAID"
    assert consumer.human_followup is True


def test_sheet_sync_failure_does_not_lose_db_result(db_session):
    _seed_queue(db_session)
    job = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=JOB_DATE).one()
    acquire_job_lock(db_session, job)
    from app.calling_agent import create_attempt

    consumer = db_session.query(Consumer).filter_by(consumer_no="CN-001").one()
    attempt = create_attempt(db_session, job, consumer)

    class BrokenSheetRepo:
        def update_row_by_consumer_no(self, *a, **kw):
            raise RuntimeError("Google Sheets API timeout")

    decision = CallDecision(intent=CustomerIntent.WILL_PAY_TODAY, promise_to_pay_date=dt.date(2026, 8, 10))
    finalize_call_attempt(db_session, attempt, decision, "[]", 20, CallState.COMPLETED, sheet_repo=BrokenSheetRepo())

    db_session.refresh(attempt)
    assert attempt.sheet_synced is False
    assert "timeout" in attempt.sheet_sync_error
    # The DB record itself must still be fully saved despite the sheet failure.
    assert attempt.call_outcome == CustomerIntent.WILL_PAY_TODAY.value
    db_session.refresh(consumer)
    assert consumer.call_status == CallState.COMPLETED.value


def test_retry_pending_sheet_syncs_recovers_after_outage(db_session):
    _seed_queue(db_session)
    job = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=JOB_DATE).one()
    acquire_job_lock(db_session, job)
    from app.calling_agent import create_attempt

    consumer = db_session.query(Consumer).filter_by(consumer_no="CN-001").one()
    attempt = create_attempt(db_session, job, consumer)

    class BrokenSheetRepo:
        def update_row_by_consumer_no(self, *a, **kw):
            raise RuntimeError("outage")

    decision = CallDecision(intent=CustomerIntent.PROMISE_TO_PAY)
    finalize_call_attempt(db_session, attempt, decision, "[]", 20, CallState.COMPLETED, sheet_repo=BrokenSheetRepo())
    db_session.refresh(attempt)
    assert attempt.sheet_synced is False

    working_repo = GoogleSheetRepository(make_sample_worksheet())
    synced_count = retry_pending_sheet_syncs(db_session, working_repo)

    assert synced_count == 1
    db_session.refresh(attempt)
    assert attempt.sheet_synced is True


def test_missing_telephony_provider_fails_before_locking_the_job(db_session, monkeypatch):
    """A live-calling attempt with no TelephonyProvider available must fail
    before acquiring the job lock or creating a CallAttempt -- otherwise the
    job is left locked forever with no finalize_call_attempt ever reached to
    release it (see app/calling_agent.py process_next_consumer)."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("TEST_MODE", "true")
    from app.config import get_settings

    get_settings.cache_clear()

    _seed_queue(db_session)
    job = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=JOB_DATE).one()
    assert job.locked_at is None
    assert job.attempt_count == 0

    with pytest.raises(Exception):
        process_next_consumer(db_session, telephony_provider=None, job_date=JOB_DATE)

    db_session.refresh(job)
    assert job.locked_at is None  # never acquired -- not left dangling
    assert job.attempt_count == 0  # no orphaned CallAttempt was created

    attempts_created = db_session.query(CallAttempt).filter_by(consumer_no="CN-001").count()
    assert attempts_created == 0
