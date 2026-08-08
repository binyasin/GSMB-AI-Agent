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
