from __future__ import annotations

import datetime as dt

import pytest
from zoneinfo import ZoneInfo

from app.config import Settings
from app.models import CallJob
from app.scheduler import (
    compute_state,
    get_campaign_status,
    recover_stale_locks,
    record_state_transition,
    set_campaign_status,
    should_place_calls,
)
from app.schemas import CallState, CampaignStatus, SchedulerStatus

TZ = ZoneInfo("Asia/Karachi")


def _settings() -> Settings:
    return Settings(
        call_start_1="09:20", call_end_1="12:45",
        call_start_2="14:20", call_end_2="17:20",
        call_start_3="17:45", call_end_3="18:30",
    )


def _at(hh: int, mm: int) -> dt.datetime:
    return dt.datetime(2026, 8, 10, hh, mm, tzinfo=TZ)


@pytest.mark.parametrize(
    "hh,mm,expected",
    [
        (9, 19, SchedulerStatus.WAITING),
        (9, 20, SchedulerStatus.CALLING),
        (12, 44, SchedulerStatus.CALLING),
        (12, 45, SchedulerStatus.BREAK),
        (14, 19, SchedulerStatus.BREAK),
        (14, 20, SchedulerStatus.CALLING),
        (17, 19, SchedulerStatus.CALLING),
        (17, 20, SchedulerStatus.BREAK),
        (17, 44, SchedulerStatus.BREAK),
        (17, 45, SchedulerStatus.CALLING),
        (18, 29, SchedulerStatus.CALLING),
        (18, 30, SchedulerStatus.DAY_COMPLETED),
        (18, 31, SchedulerStatus.WAITING_FOR_NEXT_DAY),
        (0, 0, SchedulerStatus.WAITING),
        (23, 59, SchedulerStatus.WAITING_FOR_NEXT_DAY),
    ],
)
def test_compute_state_at_every_spec_clock_point(hh, mm, expected):
    assert compute_state(_at(hh, mm), _settings()) == expected


def test_should_place_calls_requires_both_schedule_and_campaign_running():
    settings = _settings()
    assert should_place_calls(_at(9, 20), CampaignStatus.RUNNING, settings) is True
    assert should_place_calls(_at(9, 20), CampaignStatus.PAUSED, settings) is False
    assert should_place_calls(_at(12, 45), CampaignStatus.RUNNING, settings) is False  # BREAK


def test_campaign_status_defaults_to_running_and_persists(db_session):
    assert get_campaign_status(db_session) == CampaignStatus.RUNNING
    set_campaign_status(db_session, CampaignStatus.PAUSED)
    assert get_campaign_status(db_session) == CampaignStatus.PAUSED
    set_campaign_status(db_session, CampaignStatus.RUNNING)
    assert get_campaign_status(db_session) == CampaignStatus.RUNNING


def test_record_state_transition_only_logs_on_change(db_session):
    from app.models import DailySession

    row1 = record_state_transition(db_session, SchedulerStatus.CALLING, _at(9, 20))
    assert row1 is not None
    row2 = record_state_transition(db_session, SchedulerStatus.CALLING, _at(9, 25))
    assert row2 is None  # same state, no new row
    row3 = record_state_transition(db_session, SchedulerStatus.BREAK, _at(12, 45))
    assert row3 is not None

    all_rows = db_session.query(DailySession).all()
    assert len(all_rows) == 2


# ---------------------------------------------------------------------------
# Restart recovery (spec Sec.15, Sec.44 "test server restart at each state")
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("hh,mm", [(9, 19), (9, 20), (12, 45), (14, 20), (17, 20), (17, 45), (18, 30), (18, 31)])
def test_restart_at_every_state_never_crashes_and_recovers_pure_from_time(hh, mm, db_session):
    # Restart recovery must be derivable purely from current time + DB state,
    # never from in-memory state that a restart would have wiped.
    settings = _settings()
    state = compute_state(_at(hh, mm), settings)
    assert isinstance(state, SchedulerStatus)
    recovered = recover_stale_locks(db_session, _at(hh, mm))
    assert recovered == []  # nothing stale yet


def test_restart_recovers_stale_locked_job_without_duplicating(db_session):
    from app.models import Consumer

    consumer = Consumer(consumer_no="CN-900", consumer_name="Test", mobile_number="+923001234567", outstanding_amount=1000)
    db_session.add(consumer)
    db_session.flush()

    job = CallJob(
        job_uid="job-1",
        consumer_id=consumer.id,
        consumer_no="CN-900",
        job_date=dt.date(2026, 8, 10),
        state=CallState.DIALING.value,
        attempt_count=1,
        locked_at=_at(9, 20) - dt.timedelta(minutes=30),  # locked 30 min ago, process presumably crashed
        locked_by="worker-1",
    )
    db_session.add(job)
    db_session.commit()

    recovered = recover_stale_locks(db_session, _at(9, 55), stale_after_minutes=15)
    assert len(recovered) == 1
    db_session.refresh(job)
    assert job.state == CallState.FAILED.value
    assert job.locked_at is None
    assert job.locked_by is None


def test_restart_does_not_touch_fresh_locks(db_session):
    from app.models import Consumer

    consumer = Consumer(consumer_no="CN-901", consumer_name="Test", mobile_number="+923001234567", outstanding_amount=1000)
    db_session.add(consumer)
    db_session.flush()

    job = CallJob(
        job_uid="job-2",
        consumer_id=consumer.id,
        consumer_no="CN-901",
        job_date=dt.date(2026, 8, 10),
        state=CallState.DIALING.value,
        attempt_count=1,
        locked_at=_at(9, 54),  # locked 1 minute ago — an actual in-flight call
        locked_by="worker-1",
    )
    db_session.add(job)
    db_session.commit()

    recovered = recover_stale_locks(db_session, _at(9, 55), stale_after_minutes=15)
    assert recovered == []
    db_session.refresh(job)
    assert job.state == CallState.DIALING.value
    assert job.locked_at is not None
