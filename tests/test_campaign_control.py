from __future__ import annotations

import datetime as dt

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.campaign_control import router
from app.database import get_db
from app.google_sheets import GoogleSheetRepository
from app.models import CallJob
from app.queue_manager import build_daily_queue
from app.scheduler import get_campaign_status
from app.schemas import CampaignStatus
from tests.conftest import make_sample_worksheet

JOB_DATE = dt.date(2026, 8, 10)


def _make_app(db_session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    return app


def test_start_pause_resume_stop_transitions(db_session):
    app = _make_app(db_session)
    client = TestClient(app)

    assert client.post("/campaign/pause").json()["campaign_status"] == "PAUSED"
    assert get_campaign_status(db_session) == CampaignStatus.PAUSED

    assert client.post("/campaign/resume").json()["campaign_status"] == "RUNNING"
    assert client.post("/campaign/stop").json()["campaign_status"] == "STOPPED"
    assert client.post("/campaign/start").json()["campaign_status"] == "RUNNING"


def test_control_token_required_when_configured(db_session, monkeypatch):
    monkeypatch.setenv("CONTROL_API_TOKEN", "secret-token")
    from app.config import get_settings

    get_settings.cache_clear()

    app = _make_app(db_session)
    client = TestClient(app)

    resp = client.post("/campaign/pause")
    assert resp.status_code == 401

    resp = client.post("/campaign/pause", headers={"X-Control-Token": "secret-token"})
    assert resp.status_code == 200


def test_status_endpoint_reports_scheduler_and_queue(db_session, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Karachi")
    records = GoogleSheetRepository(make_sample_worksheet()).read_rows()
    build_daily_queue(db_session, records, job_date=dt.date.today())

    app = _make_app(db_session)
    client = TestClient(app)
    resp = client.get("/campaign/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "scheduler_state" in body
    assert "campaign_status" in body
    assert body["consumers_remaining_in_queue"] >= 1


def test_next_consumer_endpoint(db_session):
    records = GoogleSheetRepository(make_sample_worksheet()).read_rows()
    build_daily_queue(db_session, records, job_date=dt.date.today())

    app = _make_app(db_session)
    client = TestClient(app)
    resp = client.get("/campaign/next-consumer")
    assert resp.status_code == 200
    assert resp.json()["consumer"]["consumer_no"] == "CN-001"


def test_next_consumer_endpoint_empty_queue(db_session):
    app = _make_app(db_session)
    client = TestClient(app)
    resp = client.get("/campaign/next-consumer")
    assert resp.json()["consumer"] is None


def test_retry_failed_resets_jobs_under_retry_limit(db_session):
    records = GoogleSheetRepository(make_sample_worksheet()).read_rows()
    build_daily_queue(db_session, records, job_date=dt.date.today())
    job = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=dt.date.today()).one()
    job.state = "FAILED"
    job.attempt_count = 1
    db_session.commit()

    app = _make_app(db_session)
    client = TestClient(app)
    resp = client.post("/campaign/retry-failed")
    assert resp.json()["reset_count"] == 1

    db_session.refresh(job)
    assert job.state == "PENDING"


def test_sync_sheet_returns_503_when_not_configured(db_session):
    app = _make_app(db_session)
    client = TestClient(app)
    resp = client.post("/campaign/sync-sheet")
    assert resp.status_code == 503


def test_generate_report_endpoint(db_session):
    app = _make_app(db_session)
    client = TestClient(app)
    resp = client.post("/campaign/report", params={"day": "2026-08-10"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["xlsx"].endswith("daily_report_2026-08-10.xlsx")


def test_test_call_endpoint_dry_run(db_session, monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("DRY_RUN", "true")
    from app.config import get_settings

    get_settings.cache_clear()

    app = _make_app(db_session)
    client = TestClient(app)
    resp = client.post("/campaign/test-call")
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "COMPLETED"


def test_test_call_endpoint_requires_test_mode(db_session, monkeypatch):
    monkeypatch.setenv("TEST_MODE", "false")
    from app.config import get_settings

    get_settings.cache_clear()

    app = _make_app(db_session)
    client = TestClient(app)
    resp = client.post("/campaign/test-call")
    assert resp.status_code == 400
