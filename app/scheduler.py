"""Daily calling schedule state machine (spec Sec.8-15, Sec.44).

`compute_state()` is a pure function of a timezone-aware timestamp and the
configured session windows — it has no side effects and no memory, so the
exact same function used by the live APScheduler loop is what restart
recovery calls on boot (spec Sec.15): there is no separate "recovery path"
to get out of sync with the live path.

Operator commands (START/PAUSE/RESUME/STOP, spec Sec.40-41) are a
independent layer on top: `compute_state` says *when* the schedule allows
calling; `AgentSetting["campaign_status"]` says whether the operator has
allowed it. A call is placed only when both agree.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import AgentSetting, CallJob, DailySession
from app.schemas import TERMINAL_STATES, CallState, CampaignStatus, SchedulerStatus

logger = logging.getLogger("scheduler")

CAMPAIGN_STATUS_KEY = "campaign_status"


def compute_state(now: dt.datetime, settings: Settings | None = None) -> SchedulerStatus:
    """The schedule-mandated state at instant `now` (evaluated at minute resolution)."""
    settings = settings or get_settings()
    sessions = settings.call_windows
    t = dt.time(hour=now.hour, minute=now.minute)

    first_start = sessions[0][0]
    last_end = sessions[-1][1]

    if t == last_end:
        return SchedulerStatus.DAY_COMPLETED
    if t > last_end:
        return SchedulerStatus.WAITING_FOR_NEXT_DAY
    if t < first_start:
        return SchedulerStatus.WAITING

    for start, end in sessions:
        if start <= t < end:
            return SchedulerStatus.CALLING

    return SchedulerStatus.BREAK


def should_place_calls(now: dt.datetime, campaign_status: CampaignStatus, settings: Settings | None = None) -> bool:
    """True only when the schedule allows calling AND the operator has not paused/stopped."""
    return compute_state(now, settings) == SchedulerStatus.CALLING and campaign_status == CampaignStatus.RUNNING


# ---------------------------------------------------------------------------
# Campaign status (operator override) persistence
# ---------------------------------------------------------------------------
def get_campaign_status(session: Session) -> CampaignStatus:
    row = session.scalar(select(AgentSetting).where(AgentSetting.key == CAMPAIGN_STATUS_KEY))
    if row is None or not row.value:
        return CampaignStatus.RUNNING
    try:
        return CampaignStatus(row.value)
    except ValueError:
        return CampaignStatus.RUNNING


def set_campaign_status(session: Session, status: CampaignStatus) -> None:
    row = session.scalar(select(AgentSetting).where(AgentSetting.key == CAMPAIGN_STATUS_KEY))
    if row is None:
        row = AgentSetting(key=CAMPAIGN_STATUS_KEY, value=status.value)
        session.add(row)
    else:
        row.value = status.value
    session.commit()


# ---------------------------------------------------------------------------
# Daily session state-transition log (also feeds the dashboard + report trigger)
# ---------------------------------------------------------------------------
def record_state_transition(session: Session, state: SchedulerStatus, at: dt.datetime) -> DailySession | None:
    """Append a DailySession row only when the state actually changed.

    Returns the new row if a transition happened, else None.
    """
    today = at.date()
    latest = session.scalar(
        select(DailySession).where(DailySession.session_date == today).order_by(DailySession.id.desc())
    )
    if latest is not None and latest.state == state.value and latest.exited_at is None:
        return None  # no change

    if latest is not None and latest.exited_at is None:
        latest.exited_at = at

    row = DailySession(session_date=today, state=state.value, entered_at=at)
    session.add(row)
    session.commit()
    logger.info("scheduler state -> %s at %s", state.value, at.isoformat())
    return row


def report_already_generated(session: Session, day: dt.date) -> bool:
    row = session.scalar(
        select(DailySession)
        .where(DailySession.session_date == day, DailySession.state == SchedulerStatus.DAY_COMPLETED.value)
        .order_by(DailySession.id.desc())
    )
    return bool(row and row.report_generated)


def mark_report_generated(session: Session, day: dt.date) -> None:
    row = session.scalar(
        select(DailySession)
        .where(DailySession.session_date == day, DailySession.state == SchedulerStatus.DAY_COMPLETED.value)
        .order_by(DailySession.id.desc())
    )
    if row is not None:
        row.report_generated = True
        session.commit()


# ---------------------------------------------------------------------------
# Restart / duplicate-call safety (spec Sec.15, Sec.17)
# ---------------------------------------------------------------------------
def recover_stale_locks(session: Session, now: dt.datetime, stale_after_minutes: int = 15) -> list[CallJob]:
    """On boot, never blindly resume a job that was mid-dial when the process died.

    A lock older than `stale_after_minutes` is treated as abandoned: the job
    is moved to FAILED (a *retryable* state, bounded by MAX_CALL_RETRIES —
    the same path a genuinely failed call takes) rather than silently
    re-queued as PENDING, so a call that actually connected right before
    the crash is not blindly re-dialed a second time without deliberate
    review of the failure path.
    """
    threshold = now - dt.timedelta(minutes=stale_after_minutes)
    terminal_values = {s.value for s in TERMINAL_STATES}
    stale_jobs = session.scalars(
        select(CallJob).where(CallJob.locked_at.isnot(None), CallJob.locked_at < threshold)
    ).all()
    recovered = []
    for job in stale_jobs:
        if job.state in terminal_values:
            continue
        job.state = CallState.FAILED.value
        job.locked_at = None
        job.locked_by = None
        recovered.append(job)
    if recovered:
        session.commit()
        logger.warning("recovered %d stale-locked call job(s) after restart", len(recovered))
    return recovered
