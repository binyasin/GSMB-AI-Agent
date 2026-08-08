from __future__ import annotations

import pytest
from twilio.request_validator import RequestValidator

from app.config import ConfigurationError, Settings
from app.telephony.twilio_provider import TwilioProvider, build_call_twiml, build_transfer_twiml


def test_build_call_twiml_connects_media_stream():
    xml = build_call_twiml("wss://example.com/webhooks/voice/media-stream", "attempt-uid-123")
    assert "<Connect>" in xml
    # Media Streams rejects a query string on the <Stream> URL (Twilio error
    # 31920) -- the URL must be bare, with attempt_uid carried as a <Parameter>.
    assert "wss://example.com/webhooks/voice/media-stream" in xml
    assert "?" not in xml.split("url=")[1].split('"')[1]
    assert 'name="attempt" value="attempt-uid-123"' in xml


def test_build_transfer_twiml_dials_human_number():
    xml = build_transfer_twiml("+923001234567")
    assert "<Dial>+923001234567</Dial>" in xml


def test_twilio_provider_requires_credentials():
    settings = Settings(twilio_account_sid=None, twilio_auth_token=None, twilio_phone_number=None, public_base_url=None)
    with pytest.raises(ConfigurationError):
        TwilioProvider(settings)


def test_twilio_provider_verifies_genuine_webhook_signature():
    settings = Settings(
        telephony_provider="twilio",
        twilio_account_sid="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        twilio_auth_token="fake_auth_token_for_offline_signature_test",
        twilio_phone_number="+15005550006",
        public_base_url="https://example.com",
    )
    provider = TwilioProvider(settings)

    url = "https://example.com/webhooks/voice/status"
    params = {"CallSid": "CA123", "CallStatus": "completed"}
    genuine_signature = RequestValidator(settings.twilio_auth_token).compute_signature(url, params)

    assert provider.verify_webhook_signature(url, params, genuine_signature) is True
    assert provider.verify_webhook_signature(url, params, "tampered-signature") is False

    tampered_params = {**params, "CallStatus": "failed"}
    assert provider.verify_webhook_signature(url, tampered_params, genuine_signature) is False


def _provider(mocker, call_recording_enabled: bool) -> TwilioProvider:
    settings = Settings(
        telephony_provider="twilio",
        twilio_account_sid="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        twilio_auth_token="fake_auth_token_for_offline_signature_test",
        twilio_phone_number="+15005550006",
        public_base_url="https://example.com",
        call_recording_enabled=call_recording_enabled,
    )
    mocker.patch("app.telephony.twilio_provider.Client")
    provider = TwilioProvider(settings)
    provider.client.calls.create.return_value = mocker.MagicMock(sid="CA123", status="queued")
    return provider


def test_make_call_passes_recording_callback_when_enabled(mocker):
    """Regression test: make_call previously set record=True but never told
    Twilio where to send the recording-ready notification, so
    /webhooks/voice/recording could never fire even with recording on."""
    provider = _provider(mocker, call_recording_enabled=True)

    provider.make_call(
        "+923001234567", "https://example.com/incoming", "https://example.com/status",
        recording_webhook_url="https://example.com/recording",
    )

    _, kwargs = provider.client.calls.create.call_args
    assert kwargs["record"] is True
    assert kwargs["recording_status_callback"] == "https://example.com/recording"
    assert kwargs["recording_status_callback_event"] == ["completed"]


def test_make_call_omits_recording_callback_when_disabled(mocker):
    provider = _provider(mocker, call_recording_enabled=False)

    provider.make_call(
        "+923001234567", "https://example.com/incoming", "https://example.com/status",
        recording_webhook_url="https://example.com/recording",
    )

    _, kwargs = provider.client.calls.create.call_args
    assert kwargs["record"] is False
    assert "recording_status_callback" not in kwargs


def test_make_call_omits_recording_callback_when_url_not_given(mocker):
    provider = _provider(mocker, call_recording_enabled=True)

    provider.make_call("+923001234567", "https://example.com/incoming", "https://example.com/status")

    _, kwargs = provider.client.calls.create.call_args
    assert "recording_status_callback" not in kwargs
