"""Voice webhook endpoints (spec Sec.38). Every route validates the Twilio
request signature before trusting any field in the payload."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.calling_agent import _sync_attempt_to_sheet, finalize_call_attempt, log_event
from app.config import ConfigurationError, get_settings
from app.database import get_db
from app.models import CallAttempt, CallJob, Consumer
from app.schemas import CallDecision, CallState, CustomerIntent
from app.telephony.base import TelephonyProvider
from app.telephony.twilio_provider import build_call_twiml
from app.utils import now_local
from app.webhooks.media_stream import pop_conversation

logger = logging.getLogger("calls")

router = APIRouter(prefix="/webhooks/voice", tags=["voice-webhooks"])

_TWILIO_STATUS_MAP = {
    "queued": CallState.QUEUED,
    "initiated": CallState.DIALING,
    "ringing": CallState.RINGING,
    "in-progress": CallState.CONNECTED,
    "answered": CallState.CONNECTED,
    "completed": CallState.COMPLETED,
    "busy": CallState.BUSY,
    "no-answer": CallState.NO_ANSWER,
    "failed": CallState.FAILED,
    "canceled": CallState.FAILED,
}

_TERMINAL_TWILIO_STATUSES = {"completed", "busy", "no-answer", "failed", "canceled"}


def get_provider() -> TelephonyProvider:
    settings = get_settings()
    try:
        if settings.telephony_provider == "twilio":
            from app.telephony.twilio_provider import TwilioProvider

            return TwilioProvider(settings)
        raise ConfigurationError(f"Unsupported TELEPHONY_PROVIDER={settings.telephony_provider!r}")
    except ConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def verify_and_extract(request: Request, provider: TelephonyProvider) -> dict:
    form = await request.form()
    params = {k: v for k, v in form.items()}
    signature = request.headers.get("X-Twilio-Signature", "")
    settings = get_settings()
    base = (settings.public_base_url or str(request.base_url).rstrip("/")).rstrip("/")
    url = f"{base}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    if not provider.verify_webhook_signature(url, params, signature):
        raise HTTPException(status_code=403, detail="invalid Twilio webhook signature")
    return params


def _get_attempt(session: Session, attempt_uid: str) -> CallAttempt:
    attempt = session.query(CallAttempt).filter_by(attempt_uid=attempt_uid).first()
    if attempt is None:
        raise HTTPException(status_code=404, detail=f"unknown attempt {attempt_uid}")
    return attempt


@router.post("/incoming")
async def incoming(
    attempt: str,
    request: Request,
    session: Session = Depends(get_db),
    provider: TelephonyProvider = Depends(get_provider),
):
    await verify_and_extract(request, provider)
    settings = get_settings()
    ws_base = (settings.public_base_url or "").replace("https://", "wss://").replace("http://", "ws://")
    stream_url = f"{ws_base}/webhooks/voice/media-stream"
    return Response(content=build_call_twiml(stream_url, attempt), media_type="application/xml")


@router.post("/status")
async def status(
    attempt: str,
    request: Request,
    session: Session = Depends(get_db),
    provider: TelephonyProvider = Depends(get_provider),
):
    params = await verify_and_extract(request, provider)
    call_status = params.get("CallStatus", "")
    call_sid = params.get("CallSid")
    duration = int(params.get("CallDuration") or 0)

    call_attempt = _get_attempt(session, attempt)
    log_event(session, call_attempt.id, "status_webhook", params, call_sid=call_sid)

    if call_attempt.result == CallState.COMPLETED.value:
        # Already finalized by the media-stream bridge when the conversation ended naturally.
        return Response(status_code=204)

    mapped_state = _TWILIO_STATUS_MAP.get(call_status, CallState.FAILED)

    if call_status not in _TERMINAL_TWILIO_STATUSES:
        call_attempt.result = mapped_state.value
        job = session.get(CallJob, call_attempt.call_job_id)
        job.state = mapped_state.value
        session.commit()
        tag = {"ringing": "CALL_RINGING", "answered": "CALL_ANSWERED", "in-progress": "CALL_ANSWERED"}.get(call_status)
        if tag:
            logger.info("%s attempt=%s call_sid=%s", tag, attempt, call_sid)
        return Response(status_code=204)

    # Terminal status without the bridge having finalized (no answer, busy,
    # failed, or the customer hung up mid-conversation).
    engine = pop_conversation(attempt)
    if engine is not None and engine.transcript:
        decision = engine.decision
        import json as _json

        transcript_json = _json.dumps([t.model_dump(mode="json") for t in engine.transcript])
    else:
        decision = CallDecision(
            intent=CustomerIntent.NO_ANSWER if call_status == "no-answer" else CustomerIntent.OTHER,
            human_followup=call_status not in ("no-answer", "busy"),
            notes=f"Call ended with Twilio status={call_status} before a full conversation was captured.",
        )
        transcript_json = "[]"

    finalize_call_attempt(session, call_attempt, decision, transcript_json, duration, mapped_state)
    return Response(status_code=204)


@router.post("/recording")
async def recording(
    attempt: str,
    request: Request,
    session: Session = Depends(get_db),
    provider: TelephonyProvider = Depends(get_provider),
):
    params = await verify_and_extract(request, provider)
    recording_url = params.get("RecordingUrl")
    call_attempt = _get_attempt(session, attempt)
    if recording_url:
        call_attempt.recording_url = recording_url
        consumer = session.get(Consumer, session.get(CallJob, call_attempt.call_job_id).consumer_id)
        consumer.recording_url = recording_url
        session.commit()
        # Recordings finish processing asynchronously, after the call (and
        # its own finalize_call_attempt sheet sync) has already completed --
        # without re-syncing here, the sheet's Recording URL column stays
        # empty forever even though the DB has it.
        logger.info("RECORDING_AVAILABLE attempt=%s call_sid=%s url=%s", attempt, call_attempt.provider_call_sid, recording_url)
        _sync_attempt_to_sheet(session, call_attempt, consumer)
        logger.info("RECORDING_SAVED attempt=%s call_sid=%s", attempt, call_attempt.provider_call_sid)
    log_event(session, call_attempt.id, "recording_webhook", params)
    return Response(status_code=204)


@router.post("/transcription")
async def transcription(
    attempt: str,
    request: Request,
    session: Session = Depends(get_db),
    provider: TelephonyProvider = Depends(get_provider),
):
    # Not used as the primary transcript source (see media_stream.py docstring —
    # our own Google Speech pipeline produces the authoritative transcript).
    # Kept as a required endpoint (spec Sec.38) and logged for audit purposes.
    params = await verify_and_extract(request, provider)
    call_attempt = _get_attempt(session, attempt)
    log_event(session, call_attempt.id, "transcription_webhook", params)
    return Response(status_code=204)
