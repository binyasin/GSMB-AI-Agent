"""Admin dashboard (spec Sec.45-47).

Runs against the same database as the FastAPI app directly (not over HTTP)
for simplicity — run it on the same machine/DB as `app.main`. Remote /
multi-host control should go through `campaign_control.py`'s HTTP API
instead (used by the OpenClaw skill).

Launch: `streamlit run app/dashboard.py`

`get_dashboard_data` is deliberately separated from the Streamlit rendering
calls below it so the data-assembly logic has real unit test coverage
(tests/test_dashboard.py) — Streamlit widget rendering itself isn't
meaningfully unit-testable, so it was instead verified by actually running
`streamlit run app/dashboard.py` (see README "Verification").
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from app.calling_agent import retry_pending_sheet_syncs, run_test_call
from app.config import ConfigurationError, get_settings
from app.database import SessionLocal, init_db
from app.models import CallAttempt, CallJob
from app.reports import compute_daily_report, generate_report_files
from app.scheduler import compute_state, get_campaign_status, set_campaign_status
from app.schemas import TERMINAL_STATES, CallState, CampaignStatus
from app.utils import now_local


def get_dashboard_data(session: Session) -> dict:
    settings = get_settings()
    now = now_local()
    today = now.date()

    scheduler_state = compute_state(now, settings)
    campaign_status = get_campaign_status(session)
    report = compute_daily_report(session, today)

    locked_job = session.query(CallJob).filter(CallJob.job_date == today, CallJob.locked_at.isnot(None)).first()
    remaining = (
        session.query(CallJob)
        .filter(
            CallJob.job_date == today,
            CallJob.state.notin_([s.value for s in TERMINAL_STATES]),
            CallJob.locked_at.is_(None),
        )
        .count()
    )
    recent_attempts = (
        session.query(CallAttempt).filter(CallAttempt.attempt_date == today).order_by(CallAttempt.id.desc()).limit(15).all()
    )

    return {
        "now": now,
        "today": today,
        "scheduler_state": scheduler_state,
        "campaign_status": campaign_status,
        "current_consumer_no": locked_job.consumer_no if locked_job else None,
        "consumers_remaining": remaining,
        "report": report,
        "recent_attempts": recent_attempts,
    }


def _render() -> None:  # pragma: no cover - Streamlit UI, exercised by manual run only
    import streamlit as st

    st.set_page_config(page_title="GSM Brothers Recovery Agent", layout="wide")
    init_db()
    session = SessionLocal()
    try:
        data = get_dashboard_data(session)

        st.title("GSM Brothers — AI Recovery Calling Agent")
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Date", data["today"].isoformat())
        col2.metric("Pakistan Time", data["now"].strftime("%H:%M:%S"))
        col3.metric("Scheduler State", data["scheduler_state"].value)
        col4.metric("Campaign Status", data["campaign_status"].value)

        st.subheader("Controls")
        b1, b2, b3, b4, b5, b6, b7, b8 = st.columns(8)
        if b1.button("START"):
            set_campaign_status(session, CampaignStatus.RUNNING)
            st.rerun()
        if b2.button("PAUSE"):
            set_campaign_status(session, CampaignStatus.PAUSED)
            st.rerun()
        if b3.button("RESUME"):
            set_campaign_status(session, CampaignStatus.RUNNING)
            st.rerun()
        if b4.button("STOP"):
            set_campaign_status(session, CampaignStatus.STOPPED)
            st.rerun()
        if b5.button("RETRY FAILED"):
            settings = get_settings()
            jobs = (
                session.query(CallJob)
                .filter(CallJob.job_date == data["today"], CallJob.state == CallState.FAILED.value, CallJob.attempt_count < settings.max_call_retries)
                .all()
            )
            for job in jobs:
                job.state = CallState.PENDING.value
            session.commit()
            st.success(f"Reset {len(jobs)} failed job(s) for retry")
        if b6.button("SYNC GOOGLE SHEET"):
            try:
                settings = get_settings()
                settings.require_google_sheets()
                from app.google_sheets import GoogleSheetRepository, open_worksheet

                synced = retry_pending_sheet_syncs(session, GoogleSheetRepository(open_worksheet(settings)))
                st.success(f"Synced {synced} pending record(s)")
            except ConfigurationError as exc:
                st.error(str(exc))
        if b7.button("TEST CALL"):
            try:
                attempt = run_test_call(session)
                st.success(f"Test call attempt {attempt.attempt_uid}: {attempt.result}")
            except (PermissionError, ConfigurationError) as exc:
                st.error(str(exc))
        if b8.button("GENERATE REPORT"):
            xlsx_path, csv_path = generate_report_files(session, data["today"])
            st.success(f"Report written: {xlsx_path.name}, {csv_path.name}")

        st.subheader("Today's Numbers")
        r = data["report"]
        metric_cols = st.columns(6)
        metrics = [
            ("Eligible Consumers", r.eligible_consumers),
            ("Calls Attempted", r.calls_attempted),
            ("Calls Completed", r.calls_completed),
            ("No Answer", r.no_answer),
            ("Promise to Pay", r.promise_to_pay),
            ("Human Follow-up", r.human_followup),
        ]
        for col, (label, value) in zip(metric_cols, metrics):
            col.metric(label, value)

        st.subheader("Live Activity")
        st.write(f"Current consumer: **{data['current_consumer_no'] or 'None'}**")
        st.write(f"Consumers remaining in today's queue: **{data['consumers_remaining']}**")

        st.subheader("Recent Call Outcomes")
        rows = [
            {
                "Consumer No": a.consumer_no,
                "Result": a.result,
                "Outcome": a.call_outcome,
                "Duration (s)": a.call_duration_seconds,
                "Attempt #": a.attempt_number,
                "Sheet Synced": a.sheet_synced,
            }
            for a in data["recent_attempts"]
        ]
        st.dataframe(rows, use_container_width=True)
    finally:
        session.close()


if __name__ == "__main__":  # pragma: no cover
    _render()
