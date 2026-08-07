"""Builds the daily calling queue from Google Sheet data + local DB state.

Eligibility rules (spec Sec.7): skip Already Paid=YES, Do Not Call=YES
(sheet OR the local DNC registry — the registry wins even if a sheet edit
reverts the flag), Call Status=Completed, invalid/missing mobile number, or
a call_job already in a terminal state / retry-exhausted state for today.

Never deletes or overwrites historical rows — `sync_consumers_to_db` upserts
the mutable sheet-sourced fields onto the local Consumer mirror only.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import CallJob, Consumer, DoNotCall
from app.schemas import RETRYABLE_STATES, TERMINAL_STATES, AlreadyPaidStatus, CallState, ConsumerRecord
from app.utils import is_valid_pakistani_mobile


def sync_consumers_to_db(session: Session, records: list[ConsumerRecord]) -> None:
    """Upsert each sheet row onto the local Consumer mirror, by consumer_no."""
    existing = {c.consumer_no: c for c in session.scalars(select(Consumer))}
    for record in records:
        consumer = existing.get(record.consumer_no)
        if consumer is None:
            consumer = Consumer(consumer_no=record.consumer_no)
            session.add(consumer)
            existing[record.consumer_no] = consumer

        consumer.consumer_name = record.consumer_name
        consumer.father_name = record.father_name
        consumer.mobile_number = record.mobile_number
        consumer.address = record.address
        consumer.outstanding_amount = record.outstanding_amount
        consumer.current_bill = record.current_bill
        consumer.arrears = record.arrears
        consumer.due_date = record.due_date
        consumer.tariff = record.tariff
        consumer.installment_eligible = record.installment_eligible
        consumer.installment_details = record.installment_details
        consumer.scheme_available = record.scheme_available
        consumer.scheme_description = record.scheme_description
        consumer.status = record.status
        consumer.already_paid = record.already_paid.value
        consumer.promise_to_pay_date = record.promise_to_pay_date
        consumer.remarks = record.remarks
        consumer.call_attempt = record.call_attempt
        consumer.call_status = record.call_status or consumer.call_status or "PENDING"
        consumer.call_outcome = record.call_outcome
        consumer.call_duration = record.call_duration
        consumer.transcript = record.transcript
        consumer.recording_url = record.recording_url
        consumer.last_call_date = record.last_call_date
        consumer.agent_notes = record.agent_notes
        consumer.human_followup = record.human_followup
        consumer.do_not_call = record.do_not_call

    session.flush()


def _dnc_consumer_numbers(session: Session) -> set[str]:
    return {row.consumer_no for row in session.scalars(select(DoNotCall))}


def skip_reason(consumer: Consumer, dnc_numbers: set[str], job_date: dt.date, session: Session, max_retries: int) -> str | None:
    """Return a human-readable skip reason, or None if the consumer is eligible."""
    if consumer.already_paid == AlreadyPaidStatus.YES.value:
        return "ALREADY_PAID"
    if consumer.do_not_call or consumer.consumer_no in dnc_numbers:
        return "DO_NOT_CALL"
    if (consumer.call_status or "").strip().upper() == "COMPLETED":
        return "CALL_STATUS_COMPLETED"
    if not is_valid_pakistani_mobile(consumer.mobile_number):
        return "INVALID_MOBILE_NUMBER"
    if consumer.outstanding_amount is None or consumer.outstanding_amount <= 0:
        return "NO_OUTSTANDING_AMOUNT"

    existing_job = session.scalar(
        select(CallJob).where(CallJob.consumer_no == consumer.consumer_no, CallJob.job_date == job_date)
    )
    if existing_job is not None:
        if existing_job.state in {s.value for s in TERMINAL_STATES}:
            return f"JOB_TERMINAL_{existing_job.state}"
        if existing_job.state in {s.value for s in RETRYABLE_STATES} and existing_job.attempt_count >= max_retries:
            return "RETRIES_EXHAUSTED"

    return None


def get_or_create_job(session: Session, consumer: Consumer, job_date: dt.date) -> CallJob:
    job = session.scalar(
        select(CallJob).where(CallJob.consumer_no == consumer.consumer_no, CallJob.job_date == job_date)
    )
    if job is not None:
        return job
    job = CallJob(
        job_uid=str(uuid.uuid4()),
        consumer_id=consumer.id,
        consumer_no=consumer.consumer_no,
        job_date=job_date,
        state=CallState.PENDING.value,
        attempt_count=0,
    )
    session.add(job)
    session.flush()
    return job


def build_daily_queue(
    session: Session,
    records: list[ConsumerRecord],
    job_date: dt.date | None = None,
) -> list[Consumer]:
    """Sync sheet data to DB, then return the ordered list of eligible consumers
    (with a PENDING/QUEUED CallJob for `job_date` created for each)."""
    settings = get_settings()
    job_date = job_date or dt.date.today()

    sync_consumers_to_db(session, records)
    dnc_numbers = _dnc_consumer_numbers(session)

    queue: list[Consumer] = []
    for record in records:
        consumer = session.scalar(select(Consumer).where(Consumer.consumer_no == record.consumer_no))
        if consumer is None:
            continue
        reason = skip_reason(consumer, dnc_numbers, job_date, session, settings.max_call_retries)
        if reason is not None:
            continue
        get_or_create_job(session, consumer, job_date)
        queue.append(consumer)

    session.commit()
    return queue


def next_eligible_consumer(session: Session, job_date: dt.date | None = None) -> Consumer | None:
    """The next consumer in today's queue whose job hasn't been completed/locked."""
    job_date = job_date or dt.date.today()
    non_terminal = [s.value for s in CallState if s not in TERMINAL_STATES]
    job = session.scalar(
        select(CallJob)
        .where(CallJob.job_date == job_date, CallJob.state.in_(non_terminal), CallJob.locked_at.is_(None))
        .order_by(CallJob.id.asc())
    )
    if job is None:
        return None
    return session.get(Consumer, job.consumer_id)
