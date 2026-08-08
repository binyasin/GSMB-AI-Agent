from __future__ import annotations

import datetime as dt

from app.dashboard import get_dashboard_data
from app.google_sheets import GoogleSheetRepository
from app.queue_manager import build_daily_queue
from app.schemas import CampaignStatus, SchedulerStatus
from tests.conftest import make_sample_worksheet


def test_get_dashboard_data_shape(db_session):
    records = GoogleSheetRepository(make_sample_worksheet()).read_rows()
    build_daily_queue(db_session, records, job_date=dt.date.today())

    data = get_dashboard_data(db_session)

    assert isinstance(data["scheduler_state"], SchedulerStatus)
    assert isinstance(data["campaign_status"], CampaignStatus)
    assert data["consumers_remaining"] >= 1
    assert data["report"].eligible_consumers >= 1
    assert data["current_consumer_no"] is None  # nothing locked yet


def test_get_dashboard_data_shows_locked_consumer(db_session):
    from app.calling_agent import acquire_job_lock
    from app.models import CallJob

    records = GoogleSheetRepository(make_sample_worksheet()).read_rows()
    build_daily_queue(db_session, records, job_date=dt.date.today())
    job = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=dt.date.today()).one()
    acquire_job_lock(db_session, job)

    data = get_dashboard_data(db_session)
    assert data["current_consumer_no"] == "CN-001"


# ---------------------------------------------------------------------------
# START/RESUME confirmation flow when DRY_RUN=true (spec-adjacent: this is
# the exact mechanism that fabricated results for 30 real consumers in a
# past incident -- a single accidental click on a dashboard button that
# wrote directly to the DB with no confirmation and no audit trail).
#
# IMPORTANT: AppTest.from_file() re-executes dashboard.py's source directly
# (like `python app/dashboard.py`), not via the already-imported
# `app.dashboard` module object -- so patching attributes on that imported
# module has NO effect on what AppTest runs (confirmed the hard way: an
# earlier version of this test patched the wrong thing and silently hit the
# real local database instead of the isolated fixture). What DOES work is
# patching `app.database.SessionLocal`/`init_db` themselves, since
# dashboard.py's fresh `from app.database import SessionLocal` (executed by
# AppTest) resolves against that same already-imported `app.database`
# module object, picking up whatever it currently holds.
# ---------------------------------------------------------------------------
def _run_dashboard(monkeypatch, db_session):
    import app.database as database_module

    monkeypatch.setattr(database_module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(database_module, "init_db", lambda: None)
    monkeypatch.setattr(db_session, "close", lambda: None)

    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    dashboard_path = Path(__file__).resolve().parent.parent / "app" / "dashboard.py"
    at = AppTest.from_file(str(dashboard_path))
    at.run(timeout=30)
    assert not at.exception, [str(e) for e in at.exception]
    return at


def test_start_button_requires_confirmation_when_dry_run(monkeypatch, db_session):
    from app.scheduler import get_campaign_status, set_campaign_status
    from app.schemas import CampaignStatus

    monkeypatch.setenv("DRY_RUN", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    set_campaign_status(db_session, CampaignStatus.PAUSED, actor="test-setup")

    at = _run_dashboard(monkeypatch, db_session)
    at.button[0].click().run(timeout=30)  # b1 = START

    assert get_campaign_status(db_session) == CampaignStatus.PAUSED  # unchanged -- awaiting confirmation
    warning_text = " ".join(w.value for w in at.warning)
    assert "DRY_RUN is enabled" in warning_text
    assert "30 real consumers" in warning_text


def test_start_button_confirmation_actually_starts_when_confirmed(monkeypatch, db_session):
    from app.scheduler import get_campaign_status, set_campaign_status
    from app.schemas import CampaignStatus

    monkeypatch.setenv("DRY_RUN", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    set_campaign_status(db_session, CampaignStatus.PAUSED, actor="test-setup")

    at = _run_dashboard(monkeypatch, db_session)
    at.button[0].click().run(timeout=30)  # START -> shows confirmation
    at.button(key="confirm_start_yes").click().run(timeout=30)

    assert get_campaign_status(db_session) == CampaignStatus.RUNNING


def test_start_button_confirmation_cancel_leaves_status_unchanged(monkeypatch, db_session):
    from app.scheduler import get_campaign_status, set_campaign_status
    from app.schemas import CampaignStatus

    monkeypatch.setenv("DRY_RUN", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    set_campaign_status(db_session, CampaignStatus.PAUSED, actor="test-setup")

    at = _run_dashboard(monkeypatch, db_session)
    at.button[0].click().run(timeout=30)  # START -> shows confirmation
    at.button(key="confirm_start_cancel").click().run(timeout=30)

    assert get_campaign_status(db_session) == CampaignStatus.PAUSED


def test_start_button_no_confirmation_needed_when_not_dry_run(monkeypatch, db_session):
    from app.scheduler import get_campaign_status, set_campaign_status
    from app.schemas import CampaignStatus

    monkeypatch.setenv("DRY_RUN", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    set_campaign_status(db_session, CampaignStatus.PAUSED, actor="test-setup")

    at = _run_dashboard(monkeypatch, db_session)
    at.button[0].click().run(timeout=30)  # START -- goes straight through, no DRY_RUN risk

    assert get_campaign_status(db_session) == CampaignStatus.RUNNING


# ---------------------------------------------------------------------------
# CLEAR TODAY button
# ---------------------------------------------------------------------------
def _find_button(at, label):
    for b in at.button:
        if b.label == label:
            return b
    raise AssertionError(f"no button labeled {label!r}; labels: {[b.label for b in at.button]}")


def test_clear_today_button_shows_confirmation_first(monkeypatch, db_session):
    from app.calling_agent import acquire_job_lock, create_attempt, finalize_call_attempt
    from app.google_sheets import GoogleSheetRepository
    from app.models import CallJob, Consumer
    from app.queue_manager import build_daily_queue
    from app.schemas import CallDecision, CallState, CustomerIntent
    from tests.conftest import make_sample_worksheet

    today = dt.date.today()
    records = GoogleSheetRepository(make_sample_worksheet()).read_rows()
    build_daily_queue(db_session, records, job_date=today)
    job = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=today).one()
    acquire_job_lock(db_session, job)
    consumer = db_session.query(Consumer).filter_by(consumer_no="CN-001").one()
    attempt = create_attempt(db_session, job, consumer)
    finalize_call_attempt(db_session, attempt, CallDecision(intent=CustomerIntent.PROMISE_TO_PAY, promise_to_pay_date=today), "[]", 20, CallState.COMPLETED)

    at = _run_dashboard(monkeypatch, db_session)
    _find_button(at, "🗑 CLEAR TODAY").click().run(timeout=30)

    warning_text = " ".join(w.value for w in at.warning)
    assert "permanently deletes" in warning_text
    assert "1 call record" in warning_text
    # Nothing deleted yet -- still awaiting explicit confirmation.
    from app.models import CallAttempt

    assert db_session.query(CallAttempt).count() == 1


def test_clear_today_confirm_yes_clears_records(monkeypatch, db_session):
    from app.calling_agent import acquire_job_lock, create_attempt, finalize_call_attempt
    from app.google_sheets import GoogleSheetRepository
    from app.models import CallAttempt, CallJob, Consumer
    from app.queue_manager import build_daily_queue
    from app.schemas import CallDecision, CallState, CustomerIntent
    from tests.conftest import make_sample_worksheet

    today = dt.date.today()
    records = GoogleSheetRepository(make_sample_worksheet()).read_rows()
    build_daily_queue(db_session, records, job_date=today)
    job = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=today).one()
    acquire_job_lock(db_session, job)
    consumer = db_session.query(Consumer).filter_by(consumer_no="CN-001").one()
    attempt = create_attempt(db_session, job, consumer)
    finalize_call_attempt(db_session, attempt, CallDecision(intent=CustomerIntent.PROMISE_TO_PAY, promise_to_pay_date=today), "[]", 20, CallState.COMPLETED)

    at = _run_dashboard(monkeypatch, db_session)
    _find_button(at, "🗑 CLEAR TODAY").click().run(timeout=30)
    at.button(key="confirm_clear_yes").click().run(timeout=30)

    assert not at.exception, [str(e) for e in at.exception]
    assert db_session.query(CallAttempt).count() == 0
    db_session.refresh(job)
    assert job.state == CallState.PENDING.value


def test_clear_today_cancel_leaves_records_untouched(monkeypatch, db_session):
    from app.calling_agent import acquire_job_lock, create_attempt, finalize_call_attempt
    from app.google_sheets import GoogleSheetRepository
    from app.models import CallAttempt, CallJob, Consumer
    from app.queue_manager import build_daily_queue
    from app.schemas import CallDecision, CallState, CustomerIntent
    from tests.conftest import make_sample_worksheet

    today = dt.date.today()
    records = GoogleSheetRepository(make_sample_worksheet()).read_rows()
    build_daily_queue(db_session, records, job_date=today)
    job = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=today).one()
    acquire_job_lock(db_session, job)
    consumer = db_session.query(Consumer).filter_by(consumer_no="CN-001").one()
    attempt = create_attempt(db_session, job, consumer)
    finalize_call_attempt(db_session, attempt, CallDecision(intent=CustomerIntent.PROMISE_TO_PAY, promise_to_pay_date=today), "[]", 20, CallState.COMPLETED)

    at = _run_dashboard(monkeypatch, db_session)
    _find_button(at, "🗑 CLEAR TODAY").click().run(timeout=30)
    at.button(key="confirm_clear_cancel").click().run(timeout=30)

    assert db_session.query(CallAttempt).count() == 1
