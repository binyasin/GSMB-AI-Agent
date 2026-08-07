"""Tests for app.main.scheduler_tick's sync-vs-call decoupling.

Sheet sync must run whenever Sheets are configured, regardless of
campaign_status -- pausing/stopping the campaign should stop *calling*,
not stop the local DB from mirroring the sheet (the dashboard/reports
should stay accurate even while paused).
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.google_sheets import GoogleSheetRepository
from app.models import CallJob
from app.scheduler import set_campaign_status
from app.schemas import CampaignStatus
from tests.conftest import make_sample_worksheet


@pytest.fixture(autouse=True)
def _use_db_session_for_main(monkeypatch, db_session):
    """app.main.scheduler_tick opens its own SessionLocal(); point that at
    the same in-memory engine db_session uses so this test can inspect it."""
    import app.main as main_module

    monkeypatch.setattr(main_module, "SessionLocal", lambda: db_session)
    # Prevent db_session.close() (called by scheduler_tick's finally) from
    # tearing down the session other assertions still need afterward.
    monkeypatch.setattr(db_session, "close", lambda: None)


def test_sync_runs_even_when_campaign_paused(monkeypatch, db_session):
    import app.main as main_module

    monkeypatch.setattr(main_module, "_open_sheet_repo_if_configured", lambda: GoogleSheetRepository(make_sample_worksheet()))
    monkeypatch.setattr(main_module, "_build_provider_if_needed", lambda: None)

    set_campaign_status(db_session, CampaignStatus.PAUSED)

    # A fixed "now" whose hour:minute falls inside a CALLING window
    # (09:20-12:45) -- we're testing the pause gate, not the schedule gate.
    frozen_now = dt.datetime(2026, 8, 10, 10, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(main_module, "now_local", lambda: frozen_now)

    main_module.scheduler_tick()

    jobs_today = db_session.query(CallJob).all()
    assert len(jobs_today) > 0  # sheet was synced into CallJob rows despite PAUSED

    # But nothing was actually processed/locked, since the campaign is paused.
    assert all(j.locked_at is None for j in jobs_today)
    assert all(j.state == "PENDING" for j in jobs_today)


def test_calls_are_processed_when_running_and_calling(monkeypatch, db_session):
    import app.main as main_module

    monkeypatch.setattr(main_module, "_open_sheet_repo_if_configured", lambda: GoogleSheetRepository(make_sample_worksheet()))
    monkeypatch.setattr(main_module, "_build_provider_if_needed", lambda: None)
    monkeypatch.setenv("DRY_RUN", "true")
    from app.config import get_settings

    get_settings.cache_clear()

    set_campaign_status(db_session, CampaignStatus.RUNNING)
    frozen_now = dt.datetime(2026, 8, 10, 10, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(main_module, "now_local", lambda: frozen_now)

    main_module.scheduler_tick()

    processed = db_session.query(CallJob).filter(CallJob.state == "COMPLETED").all()
    assert len(processed) == 1  # exactly one consumer processed this tick (sequential)
