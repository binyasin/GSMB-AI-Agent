"""Call orchestration: dial -> verify -> inform -> classify -> persist -> sync.

Spec Sec.62 end-to-end flow. Duplicate-call protection (Sec.17) is enforced
by an atomic UPDATE...WHERE locked_at IS NULL before any dial is placed.
Spec Sec.33 ("do not lose the result if Sheets is unavailable") is enforced
by always committing the DB record first; sheet sync is best-effort with
`sheet_synced` tracked for later retry via `retry_pending_sheet_syncs`.

In DRY_RUN mode (spec Sec.43) no telephony call is placed; the full
pipeline is exercised with a simulated conversation so the queue -> call ->
persist -> sheet-update wiring can be verified for free.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.conversation_engine import ConversationEngine
from app.google_sheets import GoogleSheetRepository
from app.models import CallAttempt, CallEvent, CallJob, Consumer
from app.queue_manager import next_eligible_consumer
from app.schemas import CallDecision, CallState, ConsumerRecord, CustomerIntent, SupportedLanguage
from app.telephony.base import TelephonyProvider
from app.utils import now_local

logger = logging.getLogger("calls")

WORKER_ID = f"worker-{uuid.uuid4().hex[:8]}"


class DuplicateCallError(RuntimeError):
    pass


def _consumer_to_record(consumer: Consumer) -> ConsumerRecord:
    return ConsumerRecord(
        consumer_no=consumer.consumer_no,
        consumer_name=consumer.consumer_name,
        mobile_number=consumer.mobile_number,
        address=consumer.address,
        outstanding_amount=float(consumer.outstanding_amount) if consumer.outstanding_amount is not None else None,
        due_date=consumer.due_date,
        tariff=consumer.tariff,
        installment_eligible=consumer.installment_eligible,
        installment_details=consumer.installment_details,
        scheme_available=consumer.scheme_available,
        scheme_description=consumer.scheme_description,
    )


def acquire_job_lock(session: Session, job: CallJob) -> bool:
    """Atomic compare-and-set lock. Returns False if another worker already holds it
    (spec Sec.17 duplicate-call protection)."""
    result = session.execute(
        update(CallJob)
        .where(CallJob.id == job.id, CallJob.locked_at.is_(None))
        .values(locked_at=now_local(), locked_by=WORKER_ID)
    )
    session.commit()
    session.refresh(job)
    return result.rowcount == 1


def release_job_lock(session: Session, job: CallJob) -> None:
    job.locked_at = None
    job.locked_by = None
    session.commit()


def log_event(session: Session, call_attempt_id: int | None, event_type: str, payload: dict, call_sid: str | None = None) -> None:
    session.add(
        CallEvent(
            call_attempt_id=call_attempt_id,
            provider_call_sid=call_sid,
            event_type=event_type,
            payload_json=json.dumps(payload, default=str),
        )
    )
    session.commit()


def create_attempt(session: Session, job: CallJob, consumer: Consumer) -> CallAttempt:
    now = now_local()
    attempt_number = job.attempt_count + 1
    attempt = CallAttempt(
        attempt_uid=str(uuid.uuid4()),
        call_job_id=job.id,
        consumer_no=consumer.consumer_no,
        attempt_number=attempt_number,
        attempt_date=now.date(),
        attempt_time=now.time(),
        result=CallState.DIALING.value,
    )
    session.add(attempt)
    job.attempt_count = attempt_number
    job.state = CallState.DIALING.value
    session.commit()
    return attempt


def finalize_call_attempt(
    session: Session,
    attempt: CallAttempt,
    decision: CallDecision,
    transcript_json: str,
    duration_seconds: int,
    result_state: CallState,
    recording_url: str | None = None,
    sheet_repo: GoogleSheetRepository | None = None,
) -> None:
    """Persist the final outcome to DB (source of truth) then to the sheet
    (best-effort — never blocks or loses the DB record if the sheet call fails)."""
    attempt.result = result_state.value
    attempt.call_outcome = decision.intent.value
    attempt.call_duration_seconds = duration_seconds
    attempt.promise_to_pay_date = decision.promise_to_pay_date
    attempt.recording_url = recording_url
    attempt.transcript_json = transcript_json
    attempt.agent_notes = decision.notes
    attempt.verification_passed = decision.verification_passed
    attempt.human_followup = decision.human_followup
    attempt.do_not_call = decision.do_not_call

    job = session.get(CallJob, attempt.call_job_id)
    consumer = session.get(Consumer, job.consumer_id)

    job.state = result_state.value

    now = now_local()
    consumer.call_attempt = job.attempt_count
    consumer.call_status = result_state.value
    consumer.call_outcome = decision.intent.value
    consumer.call_duration = duration_seconds
    consumer.transcript = transcript_json
    consumer.recording_url = recording_url
    consumer.last_call_date = now.date()
    consumer.last_call_time = now.time()
    consumer.agent_notes = decision.notes
    consumer.human_followup = decision.human_followup
    consumer.do_not_call = consumer.do_not_call or decision.do_not_call
    if decision.intent == CustomerIntent.ALREADY_PAID:
        consumer.already_paid = "CUSTOMER_CLAIMS_PAID"
    if decision.promise_to_pay_date:
        consumer.promise_to_pay_date = decision.promise_to_pay_date

    if decision.do_not_call:
        from app.models import DoNotCall

        existing = session.query(DoNotCall).filter_by(consumer_no=consumer.consumer_no).first()
        if not existing:
            session.add(
                DoNotCall(consumer_no=consumer.consumer_no, mobile_number=consumer.mobile_number, source="call")
            )

    release_job_lock(session, job)
    session.commit()

    _sync_attempt_to_sheet(session, attempt, consumer, sheet_repo=sheet_repo)


def _build_sheet_updates(consumer: Consumer, attempt: CallAttempt) -> dict[str, str]:
    """Maps DB state onto the real sheet's actual columns.

    There is no dedicated Already Paid / Do Not Call / Human Follow-up
    column (see app/schemas.py module docstring) — Call Status carries an
    explicit "DO NOT CALL" marker when applicable (both for human
    visibility and so `_derive_do_not_call` can re-detect it on a future
    sheet read), and Remarks gets a bracketed summary of the flags that
    would otherwise be invisible in the sheet. The local DoNotCall DB
    registry remains the authoritative compliance mechanism regardless of
    what ends up in these text fields.
    """
    call_status = consumer.call_status or ""
    if consumer.do_not_call and "DO NOT CALL" not in call_status.upper():
        call_status = "DO NOT CALL"

    flag_notes = []
    if consumer.already_paid and consumer.already_paid != "NO":
        flag_notes.append(f"Already paid: {consumer.already_paid}")
    if consumer.human_followup:
        flag_notes.append("Human follow-up required")
    remarks = consumer.remarks or ""
    if flag_notes:
        remarks = (remarks + " " if remarks else "") + "[" + "; ".join(flag_notes) + "]"

    return {
        "Call Atempt": str(consumer.call_attempt or 0),
        "Call Status": call_status,
        "Call out come": consumer.call_outcome or "",
        "Transcript": consumer.transcript or "",
        "Recording URL": consumer.recording_url or "",
        "Call Date": consumer.last_call_date.isoformat() if consumer.last_call_date else "",
        "Call Time": consumer.last_call_time.strftime("%H:%M:%S") if consumer.last_call_time else "",
        "Promise to Pay": "YES" if consumer.promise_to_pay_date else "",
        "Date": consumer.promise_to_pay_date.isoformat() if consumer.promise_to_pay_date else "",
        "Remarks": remarks,
        "Agent Notes": consumer.agent_notes or "",
    }


def _sync_attempt_to_sheet(session: Session, attempt: CallAttempt, consumer: Consumer, sheet_repo: GoogleSheetRepository | None = None) -> None:
    settings = get_settings()
    if sheet_repo is None:
        if not settings.has_google_credentials() or not settings.google_spreadsheet_id:
            attempt.sheet_synced = False
            attempt.sheet_sync_error = "Google Sheets not configured"
            session.commit()
            return
        from app.google_sheets import open_worksheet

        try:
            sheet_repo = GoogleSheetRepository(open_worksheet(settings))
        except Exception as exc:  # pragma: no cover - requires live credentials
            logger.warning("could not open worksheet for sync: %s", exc)
            attempt.sheet_synced = False
            attempt.sheet_sync_error = str(exc)
            session.commit()
            return

    try:
        sheet_repo.update_row_by_consumer_no(consumer.consumer_no, _build_sheet_updates(consumer, attempt))
        attempt.sheet_synced = True
        attempt.sheet_sync_error = None
    except Exception as exc:  # noqa: BLE001 - never let a sheet failure lose the DB result
        logger.warning("sheet sync failed for consumer_no=%s: %s", consumer.consumer_no, exc)
        attempt.sheet_synced = False
        attempt.sheet_sync_error = str(exc)
    session.commit()


def retry_pending_sheet_syncs(session: Session, sheet_repo: GoogleSheetRepository) -> int:
    """Re-attempt sheet sync for any attempt that failed to sync earlier (spec Sec.33)."""
    pending = session.query(CallAttempt).filter_by(sheet_synced=False).all()
    synced = 0
    for attempt in pending:
        job = session.get(CallJob, attempt.call_job_id)
        consumer = session.get(Consumer, job.consumer_id)
        _sync_attempt_to_sheet(session, attempt, consumer, sheet_repo=sheet_repo)
        if attempt.sheet_synced:
            synced += 1
    return synced


# ---------------------------------------------------------------------------
# Simulated conversation (DRY_RUN / TEST_MODE, spec Sec.42-43)
# ---------------------------------------------------------------------------
DEFAULT_SIMULATED_REPLIES = [
    "Ji han, main hi hoon.",
    "Ji, main is hafte tak pay karne ki koshish karoon ga.",
    "Agle Monday tak.",
]


def simulate_conversation(
    consumer: ConsumerRecord,
    language: SupportedLanguage = SupportedLanguage.URDU,
    scripted_replies: list[str] | None = None,
) -> tuple[CallDecision, str]:
    """Drives a full ConversationEngine call using scripted customer replies
    instead of live audio. Returns (final_decision, transcript_json)."""
    engine = ConversationEngine(consumer, language=language)
    engine.start()
    for reply in scripted_replies or DEFAULT_SIMULATED_REPLIES:
        if engine.stage.value == "ENDED":
            break
        engine.respond(reply)
    transcript_json = json.dumps([t.model_dump(mode="json") for t in engine.transcript])
    return engine.decision, transcript_json


def process_next_consumer(
    session: Session,
    telephony_provider: TelephonyProvider | None = None,
    sheet_repo: GoogleSheetRepository | None = None,
    job_date: dt.date | None = None,
) -> CallAttempt | None:
    """The scheduler's per-tick unit of work: dial (or simulate) the next
    eligible consumer. Returns the CallAttempt created, or None if the
    queue is empty."""
    settings = get_settings()
    job_date = job_date or now_local().date()

    # Fail before touching any job state if live calling was requested but
    # isn't actually possible -- doing this check after acquire_job_lock()
    # would leave the job permanently locked (its lock is only released by
    # finalize_call_attempt, which we'd never reach) with no way to recover
    # it short of a process restart.
    if not settings.dry_run and telephony_provider is None:
        settings.require_twilio()
        raise NotImplementedError(
            "Live calling requires a TelephonyProvider instance (e.g. TwilioProvider) to be supplied; "
            "none was configured for this call."
        )

    consumer = next_eligible_consumer(session, job_date)
    if consumer is None:
        return None

    from sqlalchemy import select

    job = session.scalar(select(CallJob).where(CallJob.consumer_no == consumer.consumer_no, CallJob.job_date == job_date))
    if job is None:
        return None

    if not acquire_job_lock(session, job):
        logger.info("job for consumer_no=%s already locked by another worker; skipping", consumer.consumer_no)
        raise DuplicateCallError(f"call job for {consumer.consumer_no} is already locked")

    attempt = create_attempt(session, job, consumer)
    log_event(session, attempt.id, "call_attempt_created", {"consumer_no": consumer.consumer_no, "dry_run": settings.dry_run})

    to_number = consumer.mobile_number
    if settings.test_mode:
        to_number = settings.test_phone_number
        logger.info("TEST_MODE active: redirecting dial from %s to %s", consumer.mobile_number, to_number)

    if settings.dry_run:
        decision, transcript_json = simulate_conversation(_consumer_to_record(consumer))
        duration = 45
        finalize_call_attempt(
            session, attempt, decision, transcript_json, duration, CallState.COMPLETED,
            recording_url=None, sheet_repo=sheet_repo,
        )
        log_event(session, attempt.id, "dry_run_simulated", {"intent": decision.intent.value})
        return attempt

    # telephony_provider is guaranteed non-None here (checked before any job
    # state was touched, above).
    base_url = settings.public_base_url
    voice_url = f"{base_url}/webhooks/voice/incoming?attempt={attempt.attempt_uid}"
    status_url = f"{base_url}/webhooks/voice/status?attempt={attempt.attempt_uid}"
    handle = telephony_provider.make_call(to_number, voice_url, status_url)
    attempt.provider_call_sid = handle.provider_call_sid
    attempt.result = CallState.RINGING.value
    job.state = CallState.RINGING.value
    session.commit()
    log_event(session, attempt.id, "call_placed", {"call_sid": handle.provider_call_sid}, call_sid=handle.provider_call_sid)
    return attempt


TEST_CONSUMER_NO = "TEST-CONSUMER"


def run_test_call(session: Session, telephony_provider: TelephonyProvider | None = None) -> CallAttempt:
    """Spec Sec.41 "TEST CALL" command: dials TEST_PHONE_NUMBER using the
    TEST_* fixture consumer, bypassing the daily queue entirely. Requires
    TEST_MODE=true regardless of DRY_RUN, so it can never accidentally
    target a real consumer number."""
    from sqlalchemy import select

    from app.queue_manager import get_or_create_job

    settings = get_settings()
    if not settings.test_mode:
        raise PermissionError("TEST_MODE must be enabled to run a test call (see .env TEST_MODE=true)")

    consumer = session.scalar(select(Consumer).where(Consumer.consumer_no == TEST_CONSUMER_NO))
    if consumer is None:
        consumer = Consumer(consumer_no=TEST_CONSUMER_NO)
        session.add(consumer)
    consumer.consumer_name = settings.test_consumer_name
    consumer.mobile_number = settings.test_phone_number
    consumer.outstanding_amount = float(settings.test_outstanding_amount)
    consumer.installment_eligible = True
    consumer.installment_details = settings.test_scheme
    consumer.call_status = "PENDING"
    consumer.already_paid = "NO"
    consumer.do_not_call = False
    session.flush()

    job_date = now_local().date()
    job = get_or_create_job(session, consumer, job_date)
    if job.state in {s.value for s in CallState} and job.locked_at is not None:
        release_job_lock(session, job)  # a prior test call today shouldn't block a new one
    if not acquire_job_lock(session, job):
        raise DuplicateCallError("a test call is already in progress")

    attempt = create_attempt(session, job, consumer)
    log_event(session, attempt.id, "test_call_created", {"dry_run": settings.dry_run})

    if settings.dry_run:
        decision, transcript_json = simulate_conversation(_consumer_to_record(consumer))
        finalize_call_attempt(session, attempt, decision, transcript_json, 30, CallState.COMPLETED)
        return attempt

    settings.require_twilio()
    if telephony_provider is None:
        from app.telephony.twilio_provider import TwilioProvider

        telephony_provider = TwilioProvider(settings)

    base_url = settings.public_base_url
    voice_url = f"{base_url}/webhooks/voice/incoming?attempt={attempt.attempt_uid}"
    status_url = f"{base_url}/webhooks/voice/status?attempt={attempt.attempt_uid}"
    handle = telephony_provider.make_call(settings.test_phone_number, voice_url, status_url)
    attempt.provider_call_sid = handle.provider_call_sid
    attempt.result = CallState.RINGING.value
    job.state = CallState.RINGING.value
    session.commit()
    return attempt


def clear_daily_records(session: Session, day: dt.date | None = None) -> dict:
    """Clears today's local call records — CallAttempt rows deleted, CallJob
    rows reset to PENDING/unlocked, and the touched Consumer rows' call-result
    fields reset to blank/PENDING.

    Deliberately **local-database-only**: never touches the Google Sheet (the
    authoritative consumer data source) or any consumer identity/dues fields.
    A subsequent sheet sync will simply re-populate Consumer rows from the
    sheet as usual. Refuses to run while any of today's jobs are currently
    locked (an attempt genuinely in flight), to avoid clearing state out from
    under an active call.
    """
    from sqlalchemy import select

    day = day or now_local().date()

    locked = session.scalar(
        select(CallJob).where(CallJob.job_date == day, CallJob.locked_at.isnot(None))
    )
    if locked is not None:
        raise RuntimeError(
            f"Refusing to clear: consumer_no={locked.consumer_no} has a call in progress right now."
        )

    jobs = session.query(CallJob).filter(CallJob.job_date == day).all()
    consumer_nos = {job.consumer_no for job in jobs}

    deleted_attempts = (
        session.query(CallAttempt)
        .filter(CallAttempt.attempt_date == day)
        .delete(synchronize_session=False)
    )

    for job in jobs:
        job.state = CallState.PENDING.value
        job.attempt_count = 0
        job.locked_at = None
        job.locked_by = None

    consumers = session.query(Consumer).filter(Consumer.consumer_no.in_(consumer_nos)).all() if consumer_nos else []
    for consumer in consumers:
        consumer.call_attempt = 0
        consumer.call_status = "PENDING"
        consumer.call_outcome = None
        consumer.call_duration = None
        consumer.transcript = None
        consumer.recording_url = None
        consumer.last_call_date = None
        consumer.last_call_time = None
        consumer.agent_notes = None
        consumer.promise_to_pay_date = None
        consumer.human_followup = False

    session.commit()
    logger.info("cleared daily records for %s: %d attempt(s) deleted, %d job(s) reset", day, deleted_attempts, len(jobs))
    return {"attempts_deleted": deleted_attempts, "jobs_reset": len(jobs), "consumers_reset": len(consumers)}
