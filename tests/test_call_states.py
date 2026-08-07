from __future__ import annotations

import datetime as dt

from app.calling_agent import create_attempt, finalize_call_attempt
from app.google_sheets import GoogleSheetRepository
from app.models import CallJob
from app.queue_manager import build_daily_queue
from app.schemas import (
    RETRYABLE_STATES,
    TERMINAL_STATES,
    CallDecision,
    CallState,
    CustomerIntent,
)
from tests.conftest import make_sample_worksheet

JOB_DATE = dt.date(2026, 8, 10)

ALL_STATES = {
    "PENDING", "QUEUED", "DIALING", "RINGING", "CONNECTED", "IN_PROGRESS",
    "COMPLETED", "NO_ANSWER", "BUSY", "FAILED", "CALLBACK_REQUESTED",
    "HUMAN_FOLLOWUP", "DO_NOT_CALL", "SKIPPED",
}


def test_call_state_enum_matches_spec_sec16_exactly():
    assert {s.value for s in CallState} == ALL_STATES


def test_retryable_states_are_no_answer_busy_temporary_failure():
    assert RETRYABLE_STATES == {CallState.NO_ANSWER, CallState.BUSY, CallState.FAILED}


def test_terminal_states_never_overlap_retryable_states():
    assert TERMINAL_STATES.isdisjoint(RETRYABLE_STATES)


def test_job_transitions_pending_to_dialing_to_completed(db_session):
    records = GoogleSheetRepository(make_sample_worksheet()).read_rows()
    build_daily_queue(db_session, records, job_date=JOB_DATE)
    job = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=JOB_DATE).one()
    assert job.state == CallState.PENDING.value

    from app.calling_agent import acquire_job_lock
    from app.models import Consumer

    acquire_job_lock(db_session, job)
    consumer = db_session.query(Consumer).filter_by(consumer_no="CN-001").one()
    attempt = create_attempt(db_session, job, consumer)
    db_session.refresh(job)
    assert job.state == CallState.DIALING.value
    assert job.attempt_count == 1

    decision = CallDecision(intent=CustomerIntent.WILL_PAY_TODAY, promise_to_pay_date=dt.date(2026, 8, 10))
    finalize_call_attempt(db_session, attempt, decision, "[]", 40, CallState.COMPLETED)
    db_session.refresh(job)
    assert job.state == CallState.COMPLETED.value


def test_job_can_transition_to_no_answer_and_remain_retryable(db_session):
    records = GoogleSheetRepository(make_sample_worksheet()).read_rows()
    build_daily_queue(db_session, records, job_date=JOB_DATE)
    job = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=JOB_DATE).one()

    from app.calling_agent import acquire_job_lock
    from app.models import Consumer

    acquire_job_lock(db_session, job)
    consumer = db_session.query(Consumer).filter_by(consumer_no="CN-001").one()
    attempt = create_attempt(db_session, job, consumer)
    decision = CallDecision(intent=CustomerIntent.NO_ANSWER)
    finalize_call_attempt(db_session, attempt, decision, "[]", 0, CallState.NO_ANSWER)

    db_session.refresh(job)
    assert job.state == CallState.NO_ANSWER.value
    assert CallState(job.state) in RETRYABLE_STATES
