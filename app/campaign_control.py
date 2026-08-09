"""Campaign control API (spec Sec.40-41).

The single control surface both the dashboard (Sec.45-46) and the OpenClaw
skill (Sec.40) call — commands like START CALLING / PAUSE / STATUS map
directly onto these endpoints. Protected by a shared-secret header when
CONTROL_API_TOKEN is set (recommended for anything beyond localhost use).
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.calling_agent import (
    ConcurrencyLimitReached,
    DuplicateCallError,
    place_call_for_consumer,
    retry_pending_sheet_syncs,
    run_test_call,
)
from app.config import ConfigurationError, get_settings
from app.database import get_db
from app.models import CallJob
from app.queue_manager import next_eligible_consumer
from app.reports import compute_daily_report, generate_report_files
from app.scheduler import compute_state, get_campaign_status, set_campaign_status
from app.schemas import TERMINAL_STATES, CallState, CampaignStatus
from app.utils import now_local

router = APIRouter(prefix="/campaign", tags=["campaign-control"])


def require_control_token(x_control_token: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.control_api_token:
        return  # no token configured -> control API is open (dev/local use only; see README security notes)
    if x_control_token != settings.control_api_token:
        raise HTTPException(status_code=401, detail="invalid or missing X-Control-Token header")


@router.post("/start", dependencies=[Depends(require_control_token)])
def start_campaign(session: Session = Depends(get_db)):
    set_campaign_status(session, CampaignStatus.RUNNING, actor="api")
    return {"campaign_status": CampaignStatus.RUNNING.value}


@router.post("/pause", dependencies=[Depends(require_control_token)])
def pause_campaign(session: Session = Depends(get_db)):
    set_campaign_status(session, CampaignStatus.PAUSED, actor="api")
    return {"campaign_status": CampaignStatus.PAUSED.value}


@router.post("/resume", dependencies=[Depends(require_control_token)])
def resume_campaign(session: Session = Depends(get_db)):
    set_campaign_status(session, CampaignStatus.RUNNING, actor="api")
    return {"campaign_status": CampaignStatus.RUNNING.value}


@router.post("/stop", dependencies=[Depends(require_control_token)])
def stop_campaign(session: Session = Depends(get_db)):
    set_campaign_status(session, CampaignStatus.STOPPED, actor="api")
    return {"campaign_status": CampaignStatus.STOPPED.value}


@router.get("/status", dependencies=[Depends(require_control_token)])
def campaign_status(session: Session = Depends(get_db)):
    settings = get_settings()
    now = now_local()
    today = now.date()
    scheduler_state = compute_state(now, settings)
    campaign_state = get_campaign_status(session)
    report = compute_daily_report(session, today)
    locked_job = session.query(CallJob).filter(CallJob.job_date == today, CallJob.locked_at.isnot(None)).first()
    remaining = session.query(CallJob).filter(
        CallJob.job_date == today,
        CallJob.state.notin_([s.value for s in TERMINAL_STATES]),
        CallJob.locked_at.is_(None),
    ).count()

    return {
        "now": now.isoformat(),
        "scheduler_state": scheduler_state.value,
        "campaign_status": campaign_state.value,
        "current_consumer_no": locked_job.consumer_no if locked_job else None,
        "consumers_remaining_in_queue": remaining,
        "today": report.model_dump(mode="json"),
    }


@router.get("/next-consumer", dependencies=[Depends(require_control_token)])
def next_consumer(session: Session = Depends(get_db)):
    consumer = next_eligible_consumer(session, now_local().date())
    if consumer is None:
        return {"consumer": None}
    return {
        "consumer": {
            "consumer_no": consumer.consumer_no,
            "consumer_name": consumer.consumer_name,
            "outstanding_amount": float(consumer.outstanding_amount) if consumer.outstanding_amount else None,
            "call_attempt": consumer.call_attempt,
        }
    }


@router.post("/report", dependencies=[Depends(require_control_token)])
def generate_report(day: str | None = None, session: Session = Depends(get_db)):
    target_day = dt.date.fromisoformat(day) if day else now_local().date()
    xlsx_path, csv_path = generate_report_files(session, target_day)
    return {"xlsx": str(xlsx_path), "csv": str(csv_path)}


@router.post("/retry-failed", dependencies=[Depends(require_control_token)])
def retry_failed(session: Session = Depends(get_db)):
    settings = get_settings()
    today = now_local().date()
    jobs = (
        session.query(CallJob)
        .filter(CallJob.job_date == today, CallJob.state == CallState.FAILED.value, CallJob.attempt_count < settings.max_call_retries)
        .all()
    )
    for job in jobs:
        job.state = CallState.PENDING.value
    session.commit()
    return {"reset_count": len(jobs)}


@router.post("/sync-sheet", dependencies=[Depends(require_control_token)])
def sync_sheet(session: Session = Depends(get_db)):
    settings = get_settings()
    try:
        settings.require_google_sheets()
    except ConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    from app.google_sheets import GoogleSheetRepository, open_worksheet

    sheet_repo = GoogleSheetRepository(open_worksheet(settings))
    synced = retry_pending_sheet_syncs(session, sheet_repo)
    return {"synced_count": synced}


@router.post("/test-call", dependencies=[Depends(require_control_token)])
def test_call(session: Session = Depends(get_db)):
    try:
        attempt = run_test_call(session)
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "attempt_uid": attempt.attempt_uid,
        "result": attempt.result,
        "call_outcome": attempt.call_outcome,
    }


@router.post("/call-consumer", dependencies=[Depends(require_control_token)])
def call_consumer(consumer_no: str, session: Session = Depends(get_db)):
    """Manual spot-call for one specific consumer_no's existing job today --
    bypasses queue order entirely (unlike /campaign/start, which lets the
    scheduler work through the whole queue in order). Must be run through
    this endpoint rather than a standalone script: register_conversation()
    populates an in-process registry that only this running app's own
    /webhooks/voice/media-stream handler can see."""
    settings = get_settings()
    telephony_provider = None
    if not settings.dry_run:
        try:
            settings.require_twilio()
        except ConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        from app.telephony.twilio_provider import TwilioProvider

        telephony_provider = TwilioProvider(settings)

    try:
        attempt = place_call_for_consumer(session, consumer_no, telephony_provider=telephony_provider)
    except DuplicateCallError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConcurrencyLimitReached as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    if attempt is None:
        raise HTTPException(
            status_code=404,
            detail=f"no eligible job for consumer_no={consumer_no!r} today (not in queue, already terminal, or unknown consumer)",
        )
    return {
        "attempt_uid": attempt.attempt_uid,
        "consumer_no": consumer_no,
        "result": attempt.result,
        "call_outcome": attempt.call_outcome,
    }
