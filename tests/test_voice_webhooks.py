from __future__ import annotations

import datetime as dt

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app.calling_agent import acquire_job_lock, create_attempt
from app.config import Settings
from app.database import get_db
from app.google_sheets import GoogleSheetRepository
from app.models import CallAttempt, CallJob, Consumer
from app.queue_manager import build_daily_queue
from app.schemas import CallState
from app.telephony.twilio_provider import TwilioProvider
from app.webhooks import voice
from tests.conftest import make_sample_worksheet

JOB_DATE = dt.date(2026, 8, 10)
FAKE_AUTH_TOKEN = "fake_auth_token_for_offline_signature_test"
PUBLIC_BASE_URL = "https://example.com"


def _settings() -> Settings:
    return Settings(
        telephony_provider="twilio",
        twilio_account_sid="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        twilio_auth_token=FAKE_AUTH_TOKEN,
        twilio_phone_number="+15005550006",
        public_base_url=PUBLIC_BASE_URL,
    )


def _make_app(db_session, monkeypatch):
    # get_settings() is called independently inside the request handlers (for
    # URL construction), so the process-wide settings singleton must match
    # what we sign requests with, not just the dependency-overridden provider.
    monkeypatch.setenv("TELEPHONY_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", FAKE_AUTH_TOKEN)
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15005550006")
    monkeypatch.setenv("PUBLIC_BASE_URL", PUBLIC_BASE_URL)
    from app.config import get_settings

    get_settings.cache_clear()

    app = FastAPI()
    app.include_router(voice.router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[voice.get_provider] = lambda: TwilioProvider(_settings())
    return app


def _signed_post(client: TestClient, path: str, params: dict):
    url = f"{PUBLIC_BASE_URL}{path}"
    signature = RequestValidator(FAKE_AUTH_TOKEN).compute_signature(url, params)
    return client.post(path, data=params, headers={"X-Twilio-Signature": signature})


def _seed_attempt(db_session) -> CallAttempt:
    records = GoogleSheetRepository(make_sample_worksheet()).read_rows()
    build_daily_queue(db_session, records, job_date=JOB_DATE)
    job = db_session.query(CallJob).filter_by(consumer_no="CN-001", job_date=JOB_DATE).one()
    acquire_job_lock(db_session, job)
    consumer = db_session.query(Consumer).filter_by(consumer_no="CN-001").one()
    return create_attempt(db_session, job, consumer)


def test_status_webhook_rejects_bad_signature(db_session, monkeypatch):
    attempt = _seed_attempt(db_session)
    app = _make_app(db_session, monkeypatch)
    client = TestClient(app)

    resp = client.post(
        f"/webhooks/voice/status?attempt={attempt.attempt_uid}",
        data={"CallSid": "CA1", "CallStatus": "ringing"},
        headers={"X-Twilio-Signature": "not-a-real-signature"},
    )
    assert resp.status_code == 403


def test_incoming_returns_twiml_with_media_stream_url(db_session, monkeypatch):
    attempt = _seed_attempt(db_session)
    app = _make_app(db_session, monkeypatch)
    client = TestClient(app)

    resp = _signed_post(client, f"/webhooks/voice/incoming?attempt={attempt.attempt_uid}", {"CallSid": "CA1"})
    assert resp.status_code == 200
    assert "<Connect>" in resp.text
    # attempt_uid must travel as a <Parameter>, not a <Stream> query string
    # (Twilio Media Streams rejects the handshake -- error 31920 -- otherwise).
    assert f'name="attempt" value="{attempt.attempt_uid}"' in resp.text
    assert "media-stream?" not in resp.text
    assert resp.text.startswith("<?xml")


def test_status_webhook_updates_non_terminal_state(db_session, monkeypatch):
    attempt = _seed_attempt(db_session)
    app = _make_app(db_session, monkeypatch)
    client = TestClient(app)

    resp = _signed_post(
        client,
        f"/webhooks/voice/status?attempt={attempt.attempt_uid}",
        {"CallSid": "CA1", "CallStatus": "ringing"},
    )
    assert resp.status_code == 204
    db_session.refresh(attempt)
    assert attempt.result == CallState.RINGING.value


def test_status_webhook_finalizes_no_answer_without_active_conversation(db_session, monkeypatch):
    attempt = _seed_attempt(db_session)
    app = _make_app(db_session, monkeypatch)
    client = TestClient(app)

    resp = _signed_post(
        client,
        f"/webhooks/voice/status?attempt={attempt.attempt_uid}",
        {"CallSid": "CA1", "CallStatus": "no-answer", "CallDuration": "0"},
    )
    assert resp.status_code == 204
    db_session.refresh(attempt)
    assert attempt.result == CallState.NO_ANSWER.value

    job = db_session.get(CallJob, attempt.call_job_id)
    assert job.locked_at is None  # released


def test_status_webhook_idempotent_when_already_completed(db_session, monkeypatch):
    attempt = _seed_attempt(db_session)
    attempt.result = CallState.COMPLETED.value
    db_session.commit()
    app = _make_app(db_session, monkeypatch)
    client = TestClient(app)

    resp = _signed_post(
        client,
        f"/webhooks/voice/status?attempt={attempt.attempt_uid}",
        {"CallSid": "CA1", "CallStatus": "completed", "CallDuration": "40"},
    )
    assert resp.status_code == 204
    # Should not have overwritten anything or raised.


def test_recording_webhook_sets_recording_url(db_session, monkeypatch):
    attempt = _seed_attempt(db_session)
    app = _make_app(db_session, monkeypatch)
    client = TestClient(app)

    resp = _signed_post(
        client,
        f"/webhooks/voice/recording?attempt={attempt.attempt_uid}",
        {"CallSid": "CA1", "RecordingUrl": "https://api.twilio.com/recordings/RE123"},
    )
    assert resp.status_code == 204
    db_session.refresh(attempt)
    assert attempt.recording_url == "https://api.twilio.com/recordings/RE123"


def test_recording_webhook_resyncs_sheet_with_the_new_url(db_session, monkeypatch):
    """Regression test: recordings finish processing asynchronously, after
    the call's own finalize_call_attempt sheet sync already ran -- the
    /recording webhook used to save recording_url to the DB but never push
    it to the sheet, so the sheet's Recording URL column stayed empty
    forever even though the DB had the real value."""
    attempt = _seed_attempt(db_session)
    app = _make_app(db_session, monkeypatch)
    client = TestClient(app)

    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", '{"fake": "credentials"}')
    monkeypatch.setenv("GOOGLE_SPREADSHEET_ID", "fake-spreadsheet-id")
    from app.config import get_settings

    get_settings.cache_clear()

    sheet = make_sample_worksheet()
    monkeypatch.setattr("app.google_sheets.open_worksheet", lambda settings: sheet)

    resp = _signed_post(
        client,
        f"/webhooks/voice/recording?attempt={attempt.attempt_uid}",
        {"CallSid": "CA1", "RecordingUrl": "https://api.twilio.com/recordings/RE123"},
    )
    assert resp.status_code == 204

    db_session.refresh(attempt)
    assert attempt.sheet_synced is True
    all_values = sheet.get_all_values()
    headers = all_values[0]
    row = all_values[1]  # CN-001 is the first data row in the fixture sheet
    assert row[headers.index("Recording URL")] == "https://api.twilio.com/recordings/RE123"


def test_transcription_webhook_logs_without_error(db_session, monkeypatch):
    attempt = _seed_attempt(db_session)
    app = _make_app(db_session, monkeypatch)
    client = TestClient(app)

    resp = _signed_post(
        client,
        f"/webhooks/voice/transcription?attempt={attempt.attempt_uid}",
        {"CallSid": "CA1", "TranscriptionText": "hello"},
    )
    assert resp.status_code == 204
