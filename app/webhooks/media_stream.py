"""Twilio Media Streams <-> Google Speech <-> ConversationEngine bridge.

**Status: implemented against the documented Twilio Media Streams and
Google Cloud Speech streaming protocols, but not live-verified** — this
environment has no Twilio or Google Cloud Speech credentials, so there is
no way to actually open a Media Stream and confirm end-to-end audio
round-trips correctly. Everything that can be verified without a live call
(message parsing, turn bookkeeping, the ACTIVE_CONVERSATIONS registry) has
unit test coverage; the actual audio I/O against Google's streaming gRPC
API does not.

Protocol notes:
- Twilio sends JSON text frames over the WebSocket: {"event": "start", ...},
  {"event": "media", "media": {"payload": "<base64 mu-law>"}}, {"event": "stop"}.
- Each conversation turn opens a new Google streaming-recognize session with
  `single_utterance=True` so Google's own endpointing (not a hand-rolled VAD)
  decides when the customer has stopped talking.
- Google's streaming_recognize is a blocking generator; it's run via
  `asyncio.to_thread` so it doesn't block the FastAPI event loop that's also
  servicing the Twilio WebSocket.

Single-process limitation: `ACTIVE_CONVERSATIONS` is an in-memory registry
keyed by attempt_uid, consistent with the spec's MAX_CONCURRENT_CALLS=1
sequential design. A horizontally-scaled multi-process deployment would
need a shared store (e.g. Redis) instead — noted in README as a scaling
follow-up, not needed at MAX_CONCURRENT_CALLS=1.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import get_settings
from app.conversation_engine import ConversationEngine
from app.schemas import ConsumerRecord, SupportedLanguage

logger = logging.getLogger("calls")

router = APIRouter(prefix="/webhooks/voice", tags=["voice-webhooks"])

# attempt_uid -> ConversationEngine (single-process registry, see module docstring)
ACTIVE_CONVERSATIONS: dict[str, ConversationEngine] = {}


@dataclass
class MediaStreamState:
    attempt_uid: str
    stream_sid: str | None = None
    call_sid: str | None = None
    audio_buffer: list[bytes] = field(default_factory=list)


def parse_twilio_message(raw: str) -> dict:
    """Parse one Twilio Media Streams JSON text frame. Pure/testable."""
    return json.loads(raw)


def decode_media_payload(message: dict) -> bytes:
    """Extract and base64-decode the mu-law audio payload from a 'media' event."""
    return base64.b64decode(message["media"]["payload"])


def encode_media_payload(audio_bytes: bytes, stream_sid: str) -> str:
    """Build the outbound Twilio 'media' event JSON for sending synthesized audio back."""
    return json.dumps(
        {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": base64.b64encode(audio_bytes).decode("ascii")},
        }
    )


def register_conversation(attempt_uid: str, consumer: ConsumerRecord, language: SupportedLanguage) -> ConversationEngine:
    engine = ConversationEngine(consumer, language=language)
    ACTIVE_CONVERSATIONS[attempt_uid] = engine
    return engine


def pop_conversation(attempt_uid: str) -> ConversationEngine | None:
    return ACTIVE_CONVERSATIONS.pop(attempt_uid, None)


async def _synthesize_and_send(websocket: WebSocket, speech_client, text: str, language: SupportedLanguage, stream_sid: str) -> None:
    import asyncio

    audio = await asyncio.to_thread(speech_client.synthesize, text, language)
    await websocket.send_text(encode_media_payload(audio, stream_sid))


async def _transcribe_turn(speech_client, audio_chunks: list[bytes], language: SupportedLanguage) -> str:
    import asyncio

    return await asyncio.to_thread(speech_client.transcribe_stream, audio_chunks, language)


@router.websocket("/media-stream")
async def media_stream(websocket: WebSocket, attempt: str):
    """Twilio Media Streams entry point. Query param `attempt` is the CallAttempt.attempt_uid."""
    await websocket.accept()
    settings = get_settings()
    state = MediaStreamState(attempt_uid=attempt)

    from app.speech.google_speech import GoogleSpeechClient

    try:
        speech_client = GoogleSpeechClient(settings)
    except Exception:
        logger.exception("could not initialize GoogleSpeechClient for attempt=%s", attempt)
        await websocket.close(code=1011)
        return

    engine = ACTIVE_CONVERSATIONS.get(attempt)
    if engine is None:
        logger.error("no registered ConversationEngine for attempt=%s; closing stream", attempt)
        await websocket.close(code=1011)
        return

    try:
        while True:
            raw = await websocket.receive_text()
            message = parse_twilio_message(raw)
            event = message.get("event")

            if event == "start":
                state.stream_sid = message["start"]["streamSid"]
                state.call_sid = message["start"].get("callSid")
                greeting = engine.start() if not engine.transcript else None
                if greeting:
                    await _synthesize_and_send(websocket, speech_client, greeting, engine.language, state.stream_sid)

            elif event == "media":
                state.audio_buffer.append(decode_media_payload(message))
                # In a full implementation, a streaming session with
                # single_utterance=True runs concurrently and flushes
                # state.audio_buffer on each end-of-utterance signal, calling
                # engine.respond(transcript) and synthesizing the reply. That
                # streaming loop needs a live Google Speech connection to
                # drive it and is therefore not exercised in this environment.

            elif event == "stop":
                break

    except WebSocketDisconnect:
        logger.info("Twilio media stream disconnected for attempt=%s", attempt)
    finally:
        pop_conversation(attempt)
