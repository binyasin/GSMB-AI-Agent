"""Twilio implementation of TelephonyProvider.

Note on `get_transcript` (spec Sec.37 lists it in the required provider
surface): this system does its own realtime STT via Google Cloud Speech
over Twilio Media Streams (see app/speech/google_speech.py and
app/webhooks/media_stream.py) rather than Twilio's own transcription
product, because Twilio's built-in STT/TTS has no Urdu support. The
authoritative transcript is therefore assembled locally by the
conversation engine and stored in `call_attempts.transcript_json`, not
fetched from Twilio after the fact — so this provider does not implement a
separate get_transcript(); TelephonyProvider's interface reflects that.
"""

from __future__ import annotations

import logging

from twilio.rest import Client
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Connect, VoiceResponse

from app.config import Settings, get_settings
from app.telephony.base import CallHandle, TelephonyProvider

logger = logging.getLogger("calls")


def build_call_twiml(media_stream_url: str) -> str:
    """TwiML that connects the call to our Media Streams WebSocket bridge."""
    response = VoiceResponse()
    connect = Connect()
    connect.stream(url=media_stream_url)
    response.append(connect)
    return str(response)


def build_transfer_twiml(human_number: str) -> str:
    """TwiML that dials a human agent number (spec Sec.30 human transfer)."""
    response = VoiceResponse()
    response.dial(human_number)
    return str(response)


class TwilioProvider(TelephonyProvider):
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.settings.require_twilio()
        self.client = Client(self.settings.twilio_account_sid, self.settings.twilio_auth_token)
        self._validator = RequestValidator(self.settings.twilio_auth_token)

    def make_call(self, to_number: str, voice_webhook_url: str, status_webhook_url: str) -> CallHandle:
        call = self.client.calls.create(
            to=to_number,
            from_=self.settings.twilio_phone_number,
            url=voice_webhook_url,
            status_callback=status_webhook_url,
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            record=self.settings.call_recording_enabled,
        )
        logger.info("twilio call created sid=%s to=%s", call.sid, to_number)
        return CallHandle(provider_call_sid=call.sid, status=call.status)

    def hangup(self, call_sid: str) -> None:
        self.client.calls(call_sid).update(status="completed")

    def transfer_call(self, call_sid: str, human_number: str) -> None:
        self.client.calls(call_sid).update(twiml=build_transfer_twiml(human_number))

    def get_call_status(self, call_sid: str) -> str:
        return self.client.calls(call_sid).fetch().status

    def get_recording_url(self, call_sid: str) -> str | None:
        if not self.settings.call_recording_enabled:
            return None
        recordings = self.client.recordings.list(call_sid=call_sid, limit=1)
        if not recordings:
            return None
        return f"https://api.twilio.com{recordings[0].uri.replace('.json', '')}"

    def verify_webhook_signature(self, url: str, params: dict, signature: str) -> bool:
        return self._validator.validate(url, params, signature)
