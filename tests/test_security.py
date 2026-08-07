from __future__ import annotations

from app.models import DoNotCall
from app.security import is_on_do_not_call_registry, verify_webhook_signature
from app.telephony.twilio_provider import TwilioProvider
from twilio.request_validator import RequestValidator
from app.config import Settings


def test_is_on_do_not_call_registry(db_session):
    db_session.add(DoNotCall(consumer_no="CN-DNC-1", mobile_number="+923001234567", source="call"))
    db_session.commit()

    assert is_on_do_not_call_registry(db_session, "CN-DNC-1") is True
    assert is_on_do_not_call_registry(db_session, "CN-NOT-LISTED") is False


def test_verify_webhook_signature_delegates_to_provider():
    settings = Settings(
        telephony_provider="twilio",
        twilio_account_sid="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        twilio_auth_token="fake-token-for-offline-test",
        twilio_phone_number="+15005550006",
        public_base_url="https://example.com",
    )
    provider = TwilioProvider(settings)
    url = "https://example.com/webhooks/voice/status"
    params = {"CallSid": "CA1"}
    sig = RequestValidator(settings.twilio_auth_token).compute_signature(url, params)

    assert verify_webhook_signature(provider, url, params, sig) is True
    assert verify_webhook_signature(provider, url, params, "bad-sig") is False
