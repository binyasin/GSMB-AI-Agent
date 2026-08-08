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


# ---------------------------------------------------------------------------
# Visual design: validated default palette from the project's dataviz skill
# (references/palette.md) -- categorical slot 1 (blue) for neutral/info,
# the fixed 4-color status palette for state, chart-chrome ink/surface roles
# for everything else. Status color never carries meaning alone (icon/dot +
# label, per the palette's own accessibility rule) and large numbers stay in
# ink, never in the series color.
# ---------------------------------------------------------------------------
_STATUS_HEX = {
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
    "info": "#2a78d6",
    "neutral": "#898781",
}

_SCHEDULER_STATUS = {
    "CALLING": "good",
    "BREAK": "warning",
    "DAY_COMPLETED": "info",
    "WAITING": "neutral",
    "WAITING_FOR_NEXT_DAY": "neutral",
}
_CAMPAIGN_STATUS = {"RUNNING": "good", "PAUSED": "warning", "STOPPED": "critical"}
_RESULT_STATUS = {
    "COMPLETED": "good",
    "NO_ANSWER": "warning",
    "BUSY": "warning",
    "FAILED": "critical",
    "DO_NOT_CALL": "critical",
    "RINGING": "info",
    "DIALING": "info",
    "CONNECTED": "info",
    "IN_PROGRESS": "info",
    "PENDING": "neutral",
    "QUEUED": "neutral",
    "SKIPPED": "neutral",
}
_OUTCOME_STATUS = {
    "ALREADY_PAID": "info",
    "PROMISE_TO_PAY": "good",
    "WILL_PAY_TODAY": "good",
    "WILL_PAY_TOMORROW": "good",
    "WILL_PAY_THIS_WEEK": "good",
    "DISPUTE": "serious",
    "HUMAN_ASSISTANCE": "serious",
    "INSTALLMENT_REQUEST": "warning",
    "NEEDS_MORE_TIME": "warning",
    "CALL_BACK": "warning",
    "WRONG_NUMBER": "neutral",
    "WRONG_PERSON": "neutral",
    "NOT_INTERESTED": "neutral",
    "DO_NOT_CALL": "critical",
    "VERIFICATION_FAILED": "critical",
    "NO_ANSWER": "warning",
    "BUSY": "warning",
    "VOICEMAIL": "neutral",
    "OTHER": "neutral",
}


def _inject_theme(st) -> None:
    st.markdown(
        """
        <style>
        :root {
            --gsm-surface-1: #fcfcfb;
            --gsm-surface-2: #f2f1ed;
            --gsm-page: #f9f9f7;
            --gsm-ink-primary: #0b0b0b;
            --gsm-ink-secondary: #52514e;
            --gsm-ink-muted: #898781;
            --gsm-border: rgba(11, 11, 11, 0.10);
            --gsm-good: #0ca30c;
            --gsm-warning: #a66a00;
            --gsm-serious: #b14a26;
            --gsm-critical: #d03b3b;
            --gsm-info: #2a78d6;
            --gsm-neutral: #6b6a65;
        }
        @media (prefers-color-scheme: dark) {
            :root {
                --gsm-surface-1: #1a1a19;
                --gsm-surface-2: #232322;
                --gsm-page: #0d0d0d;
                --gsm-ink-primary: #ffffff;
                --gsm-ink-secondary: #c3c2b7;
                --gsm-ink-muted: #898781;
                --gsm-border: rgba(255, 255, 255, 0.12);
                --gsm-good: #0ca30c;
                --gsm-warning: #e0a53a;
                --gsm-serious: #e0906c;
                --gsm-critical: #e66767;
                --gsm-info: #3987e5;
                --gsm-neutral: #a3a29c;
            }
        }

        html, body, [class*="css"] { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }

        .gsm-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 12px;
            padding: 20px 24px;
            border-radius: 14px;
            background: linear-gradient(135deg, var(--gsm-info) 0%, #1c5cab 100%);
            margin-bottom: 20px;
        }
        .gsm-header h1 {
            color: #ffffff;
            font-size: 1.5rem;
            font-weight: 650;
            margin: 0;
            line-height: 1.3;
        }
        .gsm-header p {
            color: rgba(255,255,255,0.85);
            margin: 2px 0 0 0;
            font-size: 0.88rem;
        }
        .gsm-badges { display: flex; gap: 8px; flex-wrap: wrap; }

        .gsm-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 6px 12px;
            border-radius: 999px;
            background: rgba(255,255,255,0.16);
            color: #ffffff;
            font-size: 0.82rem;
            font-weight: 600;
            white-space: nowrap;
        }
        .gsm-badge .gsm-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: currentColor;
            box-shadow: 0 0 0 3px rgba(255,255,255,0.25);
        }
        .gsm-badge.good .gsm-dot { color: #7be07b; }
        .gsm-badge.warning .gsm-dot { color: #ffd166; }
        .gsm-badge.serious .gsm-dot { color: #ffab84; }
        .gsm-badge.critical .gsm-dot { color: #ff9a9a; }
        .gsm-badge.info .gsm-dot { color: #a9cdf4; }
        .gsm-badge.neutral .gsm-dot { color: #e5e4de; }

        .gsm-section-title {
            font-size: 0.95rem;
            font-weight: 650;
            color: var(--gsm-ink-primary);
            margin: 22px 0 10px 0;
            padding-bottom: 6px;
            border-bottom: 1px solid var(--gsm-border);
        }

        .gsm-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 12px;
            margin-bottom: 4px;
        }
        .gsm-card {
            background: var(--gsm-surface-1);
            border: 1px solid var(--gsm-border);
            border-top: 3px solid var(--gsm-card-accent, var(--gsm-neutral));
            border-radius: 10px;
            padding: 14px 16px;
        }
        .gsm-card .gsm-card-label {
            font-size: 0.78rem;
            color: var(--gsm-ink-secondary);
            margin-bottom: 4px;
            font-weight: 500;
        }
        .gsm-card .gsm-card-value {
            font-size: 1.7rem;
            font-weight: 650;
            color: var(--gsm-ink-primary);
            line-height: 1.15;
        }

        .gsm-panel {
            background: var(--gsm-surface-1);
            border: 1px solid var(--gsm-border);
            border-radius: 10px;
            padding: 16px 18px;
            margin-bottom: 4px;
        }
        .gsm-panel .gsm-row { display: flex; justify-content: space-between; padding: 5px 0; font-size: 0.92rem; }
        .gsm-panel .gsm-row .gsm-label { color: var(--gsm-ink-secondary); }
        .gsm-panel .gsm-row .gsm-value { color: var(--gsm-ink-primary); font-weight: 600; }

        table.gsm-table { width: 100%; border-collapse: collapse; font-size: 0.87rem; }
        table.gsm-table th {
            text-align: left;
            color: var(--gsm-ink-muted);
            font-weight: 600;
            font-size: 0.76rem;
            text-transform: uppercase;
            letter-spacing: 0.02em;
            padding: 8px 10px;
            border-bottom: 1px solid var(--gsm-border);
        }
        table.gsm-table td {
            padding: 9px 10px;
            border-bottom: 1px solid var(--gsm-border);
            color: var(--gsm-ink-primary);
        }
        table.gsm-table tr:last-child td { border-bottom: none; }
        .gsm-pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 3px 10px;
            border-radius: 999px;
            background: var(--gsm-surface-2);
            font-size: 0.80rem;
            font-weight: 600;
            color: var(--gsm-ink-primary);
        }
        .gsm-pill .gsm-dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
        .gsm-pill.good .gsm-dot { color: var(--gsm-good); }
        .gsm-pill.warning .gsm-dot { color: var(--gsm-warning); }
        .gsm-pill.serious .gsm-dot { color: var(--gsm-serious); }
        .gsm-pill.critical .gsm-dot { color: var(--gsm-critical); }
        .gsm-pill.info .gsm-dot { color: var(--gsm-info); }
        .gsm-pill.neutral .gsm-dot { color: var(--gsm-neutral); }

        .stButton > button {
            border-radius: 8px;
            font-weight: 600;
            border: 1px solid var(--gsm-border);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _badge_html(text: str, status: str, cls: str = "gsm-badge") -> str:
    return f'<span class="{cls} {status}"><span class="gsm-dot"></span>{text}</span>'


def _stat_card(label: str, value, status: str = "neutral") -> str:
    hex_color = _STATUS_HEX.get(status, _STATUS_HEX["neutral"])
    return (
        f'<div class="gsm-card" style="--gsm-card-accent: {hex_color};">'
        f'<div class="gsm-card-label">{label}</div>'
        f'<div class="gsm-card-value">{value}</div>'
        f"</div>"
    )


def _render() -> None:  # pragma: no cover - Streamlit UI, exercised by manual run only
    import streamlit as st

    st.set_page_config(page_title="GSM Brothers Recovery Agent", layout="wide", page_icon="📞")
    _inject_theme(st)
    init_db()
    session = SessionLocal()
    try:
        data = get_dashboard_data(session)
        scheduler_key = data["scheduler_state"].value
        campaign_key = data["campaign_status"].value

        st.markdown(
            f"""
            <div class="gsm-header">
                <div>
                    <h1>GSM Brothers — AI Recovery Calling Agent</h1>
                    <p>{data['today'].isoformat()} · {data['now'].strftime('%H:%M:%S')} PKT</p>
                </div>
                <div class="gsm-badges">
                    {_badge_html('Scheduler: ' + scheduler_key, _SCHEDULER_STATUS.get(scheduler_key, 'neutral'))}
                    {_badge_html('Campaign: ' + campaign_key, _CAMPAIGN_STATUS.get(campaign_key, 'neutral'))}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.markdown('<div class="gsm-section-title">Controls</div>', unsafe_allow_html=True)
        b1, b2, b3, b4, b5, b6, b7, b8 = st.columns(8)
        dry_run_active = get_settings().dry_run

        if b1.button("▶ START", type="primary", use_container_width=True):
            if dry_run_active:
                st.session_state["confirm_campaign_start"] = True
            else:
                set_campaign_status(session, CampaignStatus.RUNNING, actor="dashboard")
                st.rerun()
        if b2.button("⏸ PAUSE", use_container_width=True):
            set_campaign_status(session, CampaignStatus.PAUSED, actor="dashboard")
            st.rerun()
        if b3.button("↻ RESUME", type="primary", use_container_width=True):
            if dry_run_active:
                st.session_state["confirm_campaign_start"] = True
            else:
                set_campaign_status(session, CampaignStatus.RUNNING, actor="dashboard")
                st.rerun()
        if b4.button("■ STOP", use_container_width=True):
            set_campaign_status(session, CampaignStatus.STOPPED, actor="dashboard")
            st.rerun()

        if st.session_state.get("confirm_campaign_start"):
            st.warning(
                f"⚠️ DRY_RUN is enabled. Starting/resuming now will immediately begin simulating "
                f"calls for all {data['consumers_remaining']} queued consumer(s) and write "
                f"FABRICATED results (no real call happens) into your real Google Sheet — this is "
                f"exactly what caused a prior incident affecting 30 real consumers. Are you sure?"
            )
            cc1, cc2 = st.columns(2)
            if cc1.button("Yes, start anyway (DRY_RUN)", key="confirm_start_yes"):
                set_campaign_status(session, CampaignStatus.RUNNING, actor="dashboard")
                st.session_state["confirm_campaign_start"] = False
                st.rerun()
            if cc2.button("Cancel", key="confirm_start_cancel"):
                st.session_state["confirm_campaign_start"] = False
                st.rerun()
        if b5.button("↺ RETRY FAILED", use_container_width=True):
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
        if b6.button("⇅ SYNC SHEET", use_container_width=True):
            try:
                settings = get_settings()
                settings.require_google_sheets()
                from app.google_sheets import GoogleSheetRepository, open_worksheet

                synced = retry_pending_sheet_syncs(session, GoogleSheetRepository(open_worksheet(settings)))
                st.success(f"Synced {synced} pending record(s)")
            except ConfigurationError as exc:
                st.error(str(exc))
        if b7.button("☎ TEST CALL", use_container_width=True):
            try:
                attempt = run_test_call(session)
                st.success(f"Test call attempt {attempt.attempt_uid}: {attempt.result}")
            except (PermissionError, ConfigurationError) as exc:
                st.error(str(exc))
        if b8.button("▤ REPORT", use_container_width=True):
            xlsx_path, csv_path = generate_report_files(session, data["today"])
            st.success(f"Report written: {xlsx_path.name}, {csv_path.name}")

        st.markdown('<div class="gsm-section-title">Today\'s Numbers</div>', unsafe_allow_html=True)
        r = data["report"]
        tiles = [
            ("Eligible Consumers", r.eligible_consumers, "info"),
            ("Calls Attempted", r.calls_attempted, "info"),
            ("Calls Completed", r.calls_completed, "good"),
            ("No Answer / Busy", r.no_answer + r.busy, "warning"),
            ("Promise to Pay", r.promise_to_pay, "good"),
            ("Human Follow-up", r.human_followup, "serious"),
            ("Do Not Call", r.do_not_call, "critical"),
            ("Failed Calls", r.failed_calls, "critical"),
        ]
        st.markdown(
            '<div class="gsm-grid">' + "".join(_stat_card(label, value, status) for label, value, status in tiles) + "</div>",
            unsafe_allow_html=True,
        )

        col_left, col_right = st.columns([1, 2])
        with col_left:
            st.markdown('<div class="gsm-section-title">Live Activity</div>', unsafe_allow_html=True)
            current = data["current_consumer_no"] or "None"
            st.markdown(
                f"""
                <div class="gsm-panel">
                    <div class="gsm-row"><span class="gsm-label">Current consumer</span><span class="gsm-value">{current}</span></div>
                    <div class="gsm-row"><span class="gsm-label">Remaining in queue</span><span class="gsm-value">{data['consumers_remaining']}</span></div>
                    <div class="gsm-row"><span class="gsm-label">Outstanding contacted</span><span class="gsm-value">Rs. {r.total_outstanding_contacted:,.0f}</span></div>
                    <div class="gsm-row"><span class="gsm-label">Promise-to-pay value</span><span class="gsm-value">Rs. {r.total_promise_to_pay_amount:,.0f}</span></div>
                    <div class="gsm-row"><span class="gsm-label">Avg. call duration</span><span class="gsm-value">{r.average_call_duration_seconds:.0f}s</span></div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        with col_right:
            st.markdown('<div class="gsm-section-title">Recent Call Outcomes</div>', unsafe_allow_html=True)
            if not data["recent_attempts"]:
                st.markdown(
                    '<div class="gsm-panel" style="text-align:center; color: var(--gsm-ink-muted);">No calls yet today.</div>',
                    unsafe_allow_html=True,
                )
            else:
                rows_html = []
                for a in data["recent_attempts"]:
                    result_status = _RESULT_STATUS.get(a.result or "", "neutral")
                    outcome_status = _OUTCOME_STATUS.get(a.call_outcome or "", "neutral")
                    synced_pill = _badge_html("Synced", "good", cls="gsm-pill") if a.sheet_synced else _badge_html("Pending", "warning", cls="gsm-pill")
                    rows_html.append(
                        f"<tr>"
                        f"<td>{a.consumer_no}</td>"
                        f"<td>{_badge_html(a.result or '—', result_status, cls='gsm-pill')}</td>"
                        f"<td>{_badge_html(a.call_outcome or '—', outcome_status, cls='gsm-pill') if a.call_outcome else '—'}</td>"
                        f"<td>{a.call_duration_seconds if a.call_duration_seconds is not None else '—'}</td>"
                        f"<td>{a.attempt_number}</td>"
                        f"<td>{synced_pill}</td>"
                        f"</tr>"
                    )
                st.markdown(
                    '<div class="gsm-panel" style="padding: 6px 12px;"><table class="gsm-table">'
                    "<tr><th>Consumer No</th><th>Result</th><th>Outcome</th><th>Duration (s)</th><th>Attempt #</th><th>Sheet</th></tr>"
                    + "".join(rows_html)
                    + "</table></div>",
                    unsafe_allow_html=True,
                )
    finally:
        session.close()


if __name__ == "__main__":  # pragma: no cover
    _render()
