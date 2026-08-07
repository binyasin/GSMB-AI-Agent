from __future__ import annotations

import datetime as dt

import openpyxl

from app.models import CallAttempt, CallJob, Consumer
from app.reports import compute_daily_report, generate_report_files
from app.schemas import CallState, CustomerIntent

DAY = dt.date(2026, 8, 10)


def _seed(session):
    consumers = []
    for i, (no, amount) in enumerate([("CN-1", 1000), ("CN-2", 2000), ("CN-3", 3000), ("CN-4", 4000)]):
        c = Consumer(consumer_no=no, consumer_name=f"Consumer {i}", outstanding_amount=amount)
        session.add(c)
        consumers.append(c)
    session.flush()

    jobs = []
    for c in consumers:
        job = CallJob(job_uid=f"job-{c.consumer_no}", consumer_id=c.id, consumer_no=c.consumer_no, job_date=DAY, state="COMPLETED")
        session.add(job)
        jobs.append(job)
    session.flush()

    attempts = [
        CallAttempt(
            attempt_uid="a1", call_job_id=jobs[0].id, consumer_no="CN-1", attempt_number=1,
            attempt_date=DAY, attempt_time=dt.time(9, 30), result=CallState.COMPLETED.value,
            call_outcome=CustomerIntent.PROMISE_TO_PAY.value, call_duration_seconds=60,
        ),
        CallAttempt(
            attempt_uid="a2", call_job_id=jobs[1].id, consumer_no="CN-2", attempt_number=1,
            attempt_date=DAY, attempt_time=dt.time(9, 40), result=CallState.NO_ANSWER.value,
        ),
        CallAttempt(
            attempt_uid="a3", call_job_id=jobs[2].id, consumer_no="CN-3", attempt_number=1,
            attempt_date=DAY, attempt_time=dt.time(9, 50), result=CallState.COMPLETED.value,
            call_outcome=CustomerIntent.ALREADY_PAID.value, call_duration_seconds=30, human_followup=True,
        ),
        CallAttempt(
            attempt_uid="a4", call_job_id=jobs[3].id, consumer_no="CN-4", attempt_number=1,
            attempt_date=DAY, attempt_time=dt.time(10, 0), result=CallState.DO_NOT_CALL.value,
            call_outcome=CustomerIntent.DO_NOT_CALL.value, do_not_call=True, call_duration_seconds=20,
        ),
    ]
    session.add_all(attempts)
    session.commit()


def test_compute_daily_report_counts(db_session):
    _seed(db_session)
    report = compute_daily_report(db_session, DAY)

    assert report.total_consumers == 4
    assert report.eligible_consumers == 4
    assert report.calls_attempted == 4
    assert report.calls_completed == 2  # a1 (promise) and a3 (already paid); a4 is DO_NOT_CALL, not COMPLETED
    assert report.no_answer == 1
    assert report.already_paid == 1
    assert report.promise_to_pay == 1
    assert report.do_not_call == 1
    assert report.human_followup == 1


def test_compute_daily_report_amounts_and_average_duration(db_session):
    _seed(db_session)
    report = compute_daily_report(db_session, DAY)

    assert report.total_outstanding_contacted == 1000 + 2000 + 3000 + 4000
    assert report.total_promise_to_pay_amount == 1000  # only CN-1 has PROMISE_TO_PAY outcome
    assert report.average_call_duration_seconds == round((60 + 30 + 20) / 3, 1)


def test_compute_daily_report_empty_day_returns_zeros(db_session):
    _seed(db_session)
    report = compute_daily_report(db_session, dt.date(2020, 1, 1))
    assert report.calls_attempted == 0
    assert report.total_outstanding_contacted == 0
    assert report.average_call_duration_seconds == 0.0


def test_generate_report_files_writes_xlsx_and_csv(db_session, tmp_path):
    _seed(db_session)
    xlsx_path, csv_path = generate_report_files(db_session, DAY, out_dir=tmp_path)

    assert xlsx_path.exists()
    assert csv_path.exists()

    workbook = openpyxl.load_workbook(xlsx_path)
    sheet = workbook.active
    values = {row[0]: row[1] for row in sheet.iter_rows(min_row=2, values_only=True)}
    assert values["Calls Attempted"] == 4
    assert values["Already Paid"] == 1

    csv_text = csv_path.read_text(encoding="utf-8")
    assert "Calls Attempted" in csv_text
