from __future__ import annotations

import base64
import json

from app.schemas import ConsumerRecord, SupportedLanguage
from app.webhooks.media_stream import (
    ACTIVE_CONVERSATIONS,
    decode_media_payload,
    encode_media_payload,
    parse_twilio_message,
    pop_conversation,
    register_conversation,
)


def test_parse_twilio_message_roundtrip():
    raw = json.dumps({"event": "start", "start": {"streamSid": "MZ123", "callSid": "CA123"}})
    parsed = parse_twilio_message(raw)
    assert parsed["event"] == "start"
    assert parsed["start"]["streamSid"] == "MZ123"


def test_decode_media_payload():
    audio_bytes = b"\x00\x01\x02\xff"
    message = {"media": {"payload": base64.b64encode(audio_bytes).decode("ascii")}}
    assert decode_media_payload(message) == audio_bytes


def test_encode_media_payload_produces_valid_twilio_event():
    audio_bytes = b"\xaa\xbb\xcc"
    raw = encode_media_payload(audio_bytes, "MZ999")
    parsed = json.loads(raw)
    assert parsed["event"] == "media"
    assert parsed["streamSid"] == "MZ999"
    assert base64.b64decode(parsed["media"]["payload"]) == audio_bytes


def test_register_and_pop_conversation():
    consumer = ConsumerRecord(consumer_no="CN-777", consumer_name="Test", outstanding_amount=1000)
    engine = register_conversation("attempt-abc", consumer, SupportedLanguage.URDU)
    assert ACTIVE_CONVERSATIONS["attempt-abc"] is engine

    popped = pop_conversation("attempt-abc")
    assert popped is engine
    assert "attempt-abc" not in ACTIVE_CONVERSATIONS


def test_pop_conversation_missing_returns_none():
    assert pop_conversation("does-not-exist") is None
