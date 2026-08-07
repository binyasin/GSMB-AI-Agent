"""Daily recovery/calling report generation (spec Sec.48-49).

`compute_daily_report` is pure DB aggregation (fully unit-testable);
`generate_report_files` writes it to `reports/daily_report_YYYY-MM-DD.xlsx`
and `.csv`.

Note on "Total Promise-to-Pay Amount": the spec doesn't define a separate
"promised amount" sheet column distinct from the account's outstanding
balance, so this uses the consumer's `outstanding_amount` at call time as
the amount attached to a promise-to-pay outcome — it is not a separate
customer-stated figure (a customer promising partial payment would need an
explicit "Promised Amount" column to track precisely; noted in README as a
possible sheet-schema extension).
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

import openpyxl
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CallAttempt, CallJob, Consumer
from app.schemas import CallState, CustomerIntent, ReportRow

_PROMISE_INTENTS = {
    CustomerIntent.PROMISE_TO_PAY.value,
    CustomerIntent.WILL_PAY_TODAY.value,
    CustomerIntent.WILL_PAY_TOMORROW.value,
    CustomerIntent.WILL_PAY_THIS_WEEK.value,
}
_WRONG_NUMBER_INTENTS = {CustomerIntent.WRONG_NUMBER.value, CustomerIntent.WRONG_PERSON.value}
_ANSWERED_STATES = {CallState.CONNECTED.value, CallState.IN_PROGRESS.value, CallState.COMPLETED.value}


def compute_daily_report(session: Session, day: dt.date) -> ReportRow:
    total_consumers = session.query(Consumer).count()
    eligible_consumers = session.query(CallJob).filter(CallJob.job_date == day).count()

    attempts = session.query(CallAttempt).filter(CallAttempt.attempt_date == day).all()
    calls_attempted = len(attempts)
    calls_answered = sum(1 for a in attempts if a.result in _ANSWERED_STATES)
    calls_completed = sum(1 for a in attempts if a.result == CallState.COMPLETED.value)
    no_answer = sum(1 for a in attempts if a.result == CallState.NO_ANSWER.value)
    busy = sum(1 for a in attempts if a.result == CallState.BUSY.value)
    failed_calls = sum(1 for a in attempts if a.result == CallState.FAILED.value)
    wrong_number = sum(1 for a in attempts if a.call_outcome in _WRONG_NUMBER_INTENTS)
    already_paid = sum(1 for a in attempts if a.call_outcome == CustomerIntent.ALREADY_PAID.value)
    promise_to_pay = sum(1 for a in attempts if a.call_outcome in _PROMISE_INTENTS)
    installment_requests = sum(1 for a in attempts if a.call_outcome == CustomerIntent.INSTALLMENT_REQUEST.value)
    call_back = sum(1 for a in attempts if a.call_outcome == CustomerIntent.CALL_BACK.value)
    disputes = sum(1 for a in attempts if a.call_outcome == CustomerIntent.DISPUTE.value)
    human_followup = sum(1 for a in attempts if a.human_followup)
    do_not_call = sum(1 for a in attempts if a.do_not_call)

    durations = [a.call_duration_seconds for a in attempts if a.call_duration_seconds is not None]
    average_call_duration = sum(durations) / len(durations) if durations else 0.0

    consumer_nos_contacted = {a.consumer_no for a in attempts}
    contacted_consumers = (
        session.scalars(select(Consumer).where(Consumer.consumer_no.in_(consumer_nos_contacted))).all()
        if consumer_nos_contacted
        else []
    )
    outstanding_by_no = {c.consumer_no: float(c.outstanding_amount or 0) for c in contacted_consumers}
    total_outstanding_contacted = sum(outstanding_by_no.values())
    total_promise_to_pay_amount = sum(
        outstanding_by_no.get(a.consumer_no, 0.0) for a in attempts if a.call_outcome in _PROMISE_INTENTS
    )

    return ReportRow(
        date=day,
        total_consumers=total_consumers,
        eligible_consumers=eligible_consumers,
        calls_attempted=calls_attempted,
        calls_answered=calls_answered,
        calls_completed=calls_completed,
        no_answer=no_answer,
        busy=busy,
        wrong_number=wrong_number,
        already_paid=already_paid,
        promise_to_pay=promise_to_pay,
        installment_requests=installment_requests,
        call_back=call_back,
        disputes=disputes,
        human_followup=human_followup,
        do_not_call=do_not_call,
        failed_calls=failed_calls,
        total_outstanding_contacted=total_outstanding_contacted,
        total_promise_to_pay_amount=total_promise_to_pay_amount,
        average_call_duration_seconds=round(average_call_duration, 1),
    )


def _report_rows(report: ReportRow) -> list[tuple[str, object]]:
    data = report.model_dump(mode="json")
    labels = {
        "date": "Date",
        "total_consumers": "Total Consumers",
        "eligible_consumers": "Eligible Consumers",
        "calls_attempted": "Calls Attempted",
        "calls_answered": "Calls Answered",
        "calls_completed": "Calls Completed",
        "no_answer": "No Answer",
        "busy": "Busy",
        "wrong_number": "Wrong Number",
        "already_paid": "Already Paid",
        "promise_to_pay": "Promise to Pay",
        "installment_requests": "Installment Requests",
        "call_back": "Call Back",
        "disputes": "Disputes",
        "human_followup": "Human Follow-up",
        "do_not_call": "Do Not Call",
        "failed_calls": "Failed Calls",
        "total_outstanding_contacted": "Total Outstanding Amount Contacted",
        "total_promise_to_pay_amount": "Total Promise-to-Pay Amount",
        "average_call_duration_seconds": "Average Call Duration (seconds)",
    }
    return [(labels[key], data[key]) for key in labels]


def generate_report_files(session: Session, day: dt.date, out_dir: str | Path = "reports") -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = compute_daily_report(session, day)
    rows = _report_rows(report)

    xlsx_path = out_dir / f"daily_report_{day.isoformat()}.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Daily Report"
    sheet.append(["Metric", "Value"])
    for label, value in rows:
        sheet.append([label, value])
    workbook.save(xlsx_path)

    csv_path = out_dir / f"daily_report_{day.isoformat()}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Metric", "Value"])
        writer.writerows(rows)

    return xlsx_path, csv_path
