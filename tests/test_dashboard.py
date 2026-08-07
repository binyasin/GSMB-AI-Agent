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
