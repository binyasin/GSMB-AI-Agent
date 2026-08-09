"""FastAPI entry point: mounts the control API + webhooks, and drives the
daily calling schedule via APScheduler.

Start: `python -m app.main` (or `uvicorn app.main:app` directly for reload/dev).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI

from app.calling_agent import ConcurrencyLimitReached, DuplicateCallError, process_next_consumer
from app.campaign_control import router as campaign_router
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.google_sheets import GoogleSheetRepository, SheetValidationError, open_worksheet
from app.logging_config import setup_logging
from app.queue_manager import build_daily_queue
from app.reports import generate_report_files
from app.scheduler import (
    compute_state,
    get_campaign_status,
    mark_report_generated,
    recover_stale_locks,
    record_state_transition,
    report_already_generated,
)
from app.schemas import CampaignStatus, SchedulerStatus
from app.telephony.base import TelephonyProvider
from app.utils import now_local
from app.webhooks.media_stream import router as media_stream_router
from app.webhooks.voice import router as voice_router

logger = logging.getLogger("scheduler")

_scheduler: BackgroundScheduler | None = None


def _open_sheet_repo_if_configured() -> GoogleSheetRepository | None:
    settings = get_settings()
    if not settings.has_google_credentials() or not settings.google_spreadsheet_id:
        return None
    try:
        return GoogleSheetRepository(open_worksheet(settings))
    except Exception:
        logger.exception("could not open Google Sheet worksheet")
        return None


def _build_provider_if_needed() -> TelephonyProvider | None:
    settings = get_settings()
    if settings.dry_run:
        return None
    try:
        settings.require_twilio()
    except Exception:
        logger.warning("telephony provider not configured; live calling unavailable")
        return None
    from app.telephony.twilio_provider import TwilioProvider

    return TwilioProvider(settings)


def scheduler_tick() -> None:
    """One iteration of the daily-schedule state machine (spec Sec.8-15).

    Sheet sync (mirroring sheet -> local DB, read-only from the sheet's
    perspective) is deliberately independent of whether the campaign is
    actively calling: it's safe and cheap, and the dashboard/reports should
    reflect fresh sheet data regardless of whether calling is paused. Only
    actually placing/simulating a call is gated behind CALLING + RUNNING.
    """
    settings = get_settings()
    now = now_local()
    state = compute_state(now, settings)

    session = SessionLocal()
    try:
        record_state_transition(session, state, now)
        campaign_status = get_campaign_status(session)

        sheet_repo = _open_sheet_repo_if_configured()
        if sheet_repo is not None:
            try:
                sheet_repo.validate_required_columns()
                records = sheet_repo.read_rows()
                build_daily_queue(session, records, job_date=now.date())
            except SheetValidationError:
                logger.exception("Google Sheet failed validation; not refreshing queue this tick")

        if state == SchedulerStatus.CALLING and campaign_status == CampaignStatus.RUNNING:
            provider = _build_provider_if_needed()
            try:
                process_next_consumer(session, telephony_provider=provider, sheet_repo=sheet_repo, job_date=now.date())
            except DuplicateCallError:
                logger.info("job already locked by another worker this tick; skipping")
            except ConcurrencyLimitReached as exc:
                logger.info("skipping this tick: %s", exc)
            except Exception:
                logger.exception("error while processing next consumer")

        elif state == SchedulerStatus.DAY_COMPLETED:
            if not report_already_generated(session, now.date()):
                try:
                    generate_report_files(session, now.date())
                    mark_report_generated(session, now.date())
                    logger.info("daily report generated for %s", now.date().isoformat())
                except Exception:
                    logger.exception("failed to generate daily report")
    finally:
        session.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    settings = get_settings()
    setup_logging()
    init_db()

    session = SessionLocal()
    try:
        recovered = recover_stale_locks(session, now_local())
        if recovered:
            logger.warning("startup recovery: released %d stale call lock(s)", len(recovered))
    finally:
        session.close()

    _scheduler = BackgroundScheduler(timezone=settings.timezone)
    _scheduler.add_job(scheduler_tick, "interval", seconds=20, id="scheduler_tick", max_instances=1, coalesce=True)
    _scheduler.start()
    logger.info("scheduler started (tick every 20s, timezone=%s)", settings.timezone)

    yield

    _scheduler.shutdown(wait=False)


app = FastAPI(title="GSM Brothers AI Recovery Calling Agent", lifespan=lifespan)
app.include_router(campaign_router)
app.include_router(voice_router)
app.include_router(media_stream_router)


@app.get("/health")
def health():
    settings = get_settings()
    return {
        "status": "ok",
        "test_mode": settings.test_mode,
        "dry_run": settings.dry_run,
        "google_sheets_configured": settings.has_google_credentials() and bool(settings.google_spreadsheet_id),
        "twilio_configured": bool(settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_phone_number),
        "ai_configured": bool(settings.ai_api_key),
        "llm_fallback_order": settings.llm_fallback_order,
        "llm_providers_configured": {
            "anthropic": bool(settings.ai_api_key),
            "deepseek": bool(settings.deepseek_api_key),
            "gemini": bool(settings.gemini_api_key),
            "openrouter": bool(settings.openrouter_api_key),
            "openai": bool(settings.openai_api_key),
        },
    }


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.app_host, port=settings.app_port, reload=False)
