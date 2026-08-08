from __future__ import annotations

import asyncio
import base64
import json
import threading

import pytest

from app.conversation_engine import ConversationEngine
from app.schemas import CallDecision, ConsumerRecord, CustomerIntent, SupportedLanguage
from app.webhooks.media_stream import (
    ACTIVE_CONVERSATIONS,
    _process_turn,
    decode_media_payload,
    encode_media_payload,
    handle_turn_result,
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


# ---------------------------------------------------------------------------
# handle_turn_result: the testable "what do we do with this transcript" logic
# ---------------------------------------------------------------------------
def _engine(classifier=None) -> ConversationEngine:
    consumer = ConsumerRecord(consumer_no="CN-1", consumer_name="Test", outstanding_amount=1000)
    engine = ConversationEngine(consumer, language=SupportedLanguage.ENGLISH, classifier=classifier or (lambda *a: CallDecision(intent=CustomerIntent.OTHER, verification_passed=True)))
    engine.start()
    return engine


@pytest.mark.anyio
async def test_handle_turn_result_none_transcript_stops_loop():
    engine = _engine()
    spoken = []

    should_continue = await handle_turn_result(engine, None, lambda text: spoken.append(text))
    assert should_continue is False
    assert spoken == []


@pytest.mark.anyio
async def test_handle_turn_result_empty_transcript_keeps_listening():
    engine = _engine()
    spoken = []

    async def speak(text):
        spoken.append(text)

    should_continue = await handle_turn_result(engine, "   ", speak)
    assert should_continue is True
    assert spoken == []  # engine.respond was never called for empty input


@pytest.mark.anyio
async def test_handle_turn_result_speaks_reply_and_continues_mid_conversation():
    engine = _engine()
    spoken = []

    async def speak(text):
        spoken.append(text)

    should_continue = await handle_turn_result(engine, "Yes speaking", speak)
    assert should_continue is True  # verification passed -> more turns expected
    assert len(spoken) == 1
    assert "outstanding" in spoken[0].lower() or "1,000" in spoken[0]


@pytest.mark.anyio
async def test_handle_turn_result_stops_when_conversation_ends():
    def classifier(stage, consumer, utterance, history):
        return CallDecision(intent=CustomerIntent.DO_NOT_CALL, do_not_call=True)

    engine = _engine(classifier=classifier)
    spoken = []

    async def speak(text):
        spoken.append(text)

    should_continue = await handle_turn_result(engine, "Don't call me again", speak)
    assert should_continue is False
    assert len(spoken) == 1


# ---------------------------------------------------------------------------
# _process_turn: real async/thread bridging, exercised with fakes standing
# in for the WebSocket and the Google Speech streaming session (the actual
# network I/O to Twilio/Google cannot be exercised without live credentials
# and a live call -- this tests the orchestration logic around it).
# ---------------------------------------------------------------------------
class FakeWebSocket:
    def __init__(self, messages: list[str]):
        self._messages = list(messages)
        self._exhausted = asyncio.Event()

    async def receive_text(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        # No more scripted messages -- behave like a real socket with nothing
        # more to deliver yet (blocks until the test tears down the task).
        await self._exhausted.wait()
        raise AssertionError("receive_text() called with no messages left")


class FakeTurnSession:
    """Blocks (in a real thread, via asyncio.to_thread) until enough chunks
    have been fed, then returns a canned transcript -- lets tests exercise
    real concurrency between the async receive loop and the background
    transcription thread instead of racing on an instantly-done task."""

    def __init__(self, finish_after_n_chunks: int = 2, transcript: str = "hello there"):
        self.fed: list[bytes] = []
        self.closed = False
        self._finish_after = finish_after_n_chunks
        self._transcript = transcript
        self._done = threading.Event()
        self.latest_transcript = ""

    def feed(self, chunk: bytes) -> None:
        self.fed.append(chunk)
        if len(self.fed) >= self._finish_after:
            self._done.set()

    def close(self) -> None:
        self.closed = True
        self._done.set()

    def run(self) -> str:
        self._done.wait(timeout=2)
        return "" if self.closed and len(self.fed) < self._finish_after else self._transcript


class FakeSpeechClient:
    def __init__(self, session: FakeTurnSession):
        self._session = session

    def open_turn_session(self, language):
        return self._session


def _media_message(payload: bytes) -> str:
    return encode_media_payload(payload, "MZ-test")


@pytest.mark.anyio
async def test_process_turn_feeds_media_and_returns_transcript():
    session = FakeTurnSession(finish_after_n_chunks=2, transcript="hello there")
    speech_client = FakeSpeechClient(session)
    ws = FakeWebSocket([_media_message(b"chunk1"), _media_message(b"chunk2")])

    transcript = await _process_turn(ws, speech_client, SupportedLanguage.ENGLISH)

    assert transcript == "hello there"
    assert session.fed == [b"chunk1", b"chunk2"]
    assert session.closed is True


@pytest.mark.anyio
async def test_process_turn_returns_none_on_stop_event():
    session = FakeTurnSession(finish_after_n_chunks=99)  # never finishes on its own
    speech_client = FakeSpeechClient(session)
    ws = FakeWebSocket([_media_message(b"chunk1"), json.dumps({"event": "stop"})])

    transcript = await _process_turn(ws, speech_client, SupportedLanguage.ENGLISH)

    assert transcript is None
    assert session.closed is True
    assert session.fed == [b"chunk1"]


@pytest.mark.anyio
async def test_process_turn_falls_back_to_interim_transcript_on_timeout():
    """Regression test: a live call (2026-08-08) confirmed 30+ seconds of
    continuous, clearly-spoken audio with no pause long enough for Google's
    endpointer to fire produced END_OF_SINGLE_UTTERANCE and got the whole
    turn discarded once the outer timeout hit. session.latest_transcript
    (populated from interim results) must be used instead of losing
    everything the caller said."""
    session = FakeTurnSession(finish_after_n_chunks=999)  # never finishes on its own
    session.latest_transcript = "meri baat abhi sun rahe hain"
    speech_client = FakeSpeechClient(session)
    ws = FakeWebSocket([])  # no messages; the turn timeout should fire first

    transcript = await _process_turn(ws, speech_client, SupportedLanguage.ENGLISH, timeout_seconds=0.05)

    assert transcript == "meri baat abhi sun rahe hain"
    assert session.closed is True
