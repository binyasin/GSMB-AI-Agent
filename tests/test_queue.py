from __future__ import annotations

import datetime as dt

from app.google_sheets import GoogleSheetRepository
from app.models import CallJob, Consumer, DoNotCall
from app.queue_manager import build_daily_queue, next_eligible_consumer
from app.schemas import CallState
from tests.conftest import make_sample_worksheet


def _records():
    return GoogleSheetRepository(make_sample_worksheet()).read_rows()


def test_queue_skips_already_paid_dnc_completed_and_invalid_number(db_session):
    queue = build_daily_queue(db_session, _records(), job_date=dt.date(2026, 8, 10))
    consumer_nos = {c.consumer_no for c in queue}

    # CN-001 valid + eligible -> included
    assert "CN-001" in consumer_nos
    # CN-002 Already Paid=YES -> excluded
    assert "CN-002" not in consumer_nos
    # CN-003 Do Not Call=YES -> excluded
    assert "CN-003" not in consumer_nos
    # CN-004 invalid mobile number -> excluded
    assert "CN-004" not in consumer_nos
    # CN-005 Call Status=COMPLETED -> excluded
    assert "CN-005" not in consumer_nos


def test_queue_creates_call_job_per_eligible_consumer(db_session):
    job_date = dt.date(2026, 8, 10)
    build_daily_queue(db_session, _records(), job_date=job_date)
    job = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=job_date).one()
    assert job.state == CallState.PENDING.value
    assert job.attempt_count == 0


def test_local_dnc_registry_overrides_sheet_even_if_sheet_flag_cleared(db_session):
    # Simulate a consumer that requested DNC previously (registry has them)
    # but the sheet's Do Not Call column was left blank/edited back to NO.
    db_session.add(DoNotCall(consumer_no="CN-001", mobile_number="+923001234567", reason="test"))
    db_session.commit()

    queue = build_daily_queue(db_session, _records(), job_date=dt.date(2026, 8, 10))
    assert "CN-001" not in {c.consumer_no for c in queue}


def test_zero_or_missing_outstanding_amount_not_eligible(db_session):
    from tests.conftest import FakeWorksheet
    from app.schemas import ALL_SHEET_COLUMNS

    headers = ALL_SHEET_COLUMNS
    row = [""] * len(headers)
    row[headers.index("Consumer No.")] = "CN-ZERO"
    row[headers.index("Name")] = "Zero Due"
    row[headers.index("Consumer Phone Number")] = "03001112233"
    row[headers.index("DUES in PKR")] = "0"
    row[headers.index("Call Status")] = "PENDING"
    ws = FakeWorksheet(headers, [row])
    records = GoogleSheetRepository(ws).read_rows()

    queue = build_daily_queue(db_session, records, job_date=dt.date(2026, 8, 10))
    assert queue == []


def test_next_eligible_consumer_returns_first_unlocked_job(db_session):
    job_date = dt.date(2026, 8, 10)
    build_daily_queue(db_session, _records(), job_date=job_date)
    consumer = next_eligible_consumer(db_session, job_date=job_date)
    assert consumer is not None
    assert consumer.consumer_no == "CN-001"


def test_next_eligible_consumer_skips_locked_job(db_session):
    job_date = dt.date(2026, 8, 10)
    build_daily_queue(db_session, _records(), job_date=job_date)
    job = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=job_date).one()
    job.locked_at = dt.datetime.now(dt.timezone.utc)
    db_session.commit()

    consumer = next_eligible_consumer(db_session, job_date=job_date)
    assert consumer is None  # CN-001 was the only eligible consumer and it's locked


def test_retries_exhausted_job_excluded_from_queue(db_session):
    job_date = dt.date(2026, 8, 10)
    build_daily_queue(db_session, _records(), job_date=job_date)
    job = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=job_date).one()
    job.state = CallState.NO_ANSWER.value
    job.attempt_count = 3  # == MAX_CALL_RETRIES default
    db_session.commit()

    queue = build_daily_queue(db_session, _records(), job_date=job_date)
    assert "CN-001" not in {c.consumer_no for c in queue}


def test_sheet_re_sync_does_not_duplicate_consumers(db_session):
    build_daily_queue(db_session, _records(), job_date=dt.date(2026, 8, 10))
    build_daily_queue(db_session, _records(), job_date=dt.date(2026, 8, 10))
    count = db_session.query(Consumer).filter_by(consumer_no="CN-001").count()
    assert count == 1
