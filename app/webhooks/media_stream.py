"""Twilio Media Streams <-> Google Speech <-> ConversationEngine bridge.

**Status: implemented against the documented Twilio Media Streams and
Google Cloud Speech streaming protocols, but not live-verified** — this
environment has no Twilio or Google Cloud Speech credentials, so there is
no way to actually open a Media Stream and confirm end-to-end audio
round-trips correctly. Everything that can be verified without a live call
(message parsing, turn bookkeeping, the ACTIVE_CONVERSATIONS registry, the
per-turn "what do we do with this transcript" decision logic in
`handle_turn_result`) has unit test coverage; the actual audio I/O against
Google's streaming gRPC API and the async/thread bridging in `_process_turn`
do not, and are the highest-risk code in this project to actually work
correctly on the first real call.

Protocol notes:
- Twilio sends JSON text frames over the WebSocket: {"event": "start", ...},
  {"event": "media", "media": {"payload": "<base64 mu-law>"}}, {"event": "stop"}.
- Each conversation turn opens a new Google streaming-recognize session
  (`GoogleSpeechClient.open_turn_session`) with `single_utterance=True` so
  Google's own endpointing -- not a hand-rolled VAD -- decides when the
  customer has stopped talking.
- Google's streaming_recognize is a blocking generator; `StreamingTurnSession.run()`
  is run via `asyncio.to_thread` so it doesn't block the FastAPI event loop
  that's also servicing the Twilio WebSocket. `_process_turn` uses
  `asyncio.wait(..., FIRST_COMPLETED)` to concurrently keep receiving
  WebSocket frames (feeding them into the session) while waiting for that
  background thread to produce a transcript.
- A few audio frames can be lost in the brief window between one turn's
  session closing and the next turn's session opening (Twilio streams
  continuously; our STT sessions are turn-based). Acceptable/known
  limitation given TEST_MODE/MAX_CONCURRENT_CALLS=1 scope; a production
  hardening pass could keep one continuous recognition session with
  server-side VAD instead of per-turn single_utterance sessions.

Single-process limitation: `ACTIVE_CONVERSATIONS` is an in-memory registry
keyed by attempt_uid, consistent with the spec's MAX_CONCURRENT_CALLS=1
sequential design. A horizontally-scaled multi-process deployment would
need a shared store (e.g. Redis) instead — noted in README as a scaling
follow-up, not needed at MAX_CONCURRENT_CALLS=1.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Awaitable, Callable
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
    audio = await asyncio.to_thread(speech_client.synthesize, text, language)
    await websocket.send_text(encode_media_payload(audio, stream_sid))


# ---------------------------------------------------------------------------
# Testable orchestration: "given a completed turn's transcript, what do we do"
# ---------------------------------------------------------------------------
async def handle_turn_result(
    engine: ConversationEngine,
    transcript: str | None,
    speak: Callable[[str], Awaitable[None]],
) -> bool:
    """Advance the conversation given one turn's transcript and speak the
    agent's reply if there is one.

    `transcript` is None when the call ended (Twilio 'stop') before this
    turn produced a transcript. Returns True if another turn should be
    listened for, False if the call loop should stop (either it already
    ended, or the conversation just reached ENDED).
    """
    if transcript is None:
        return False
    if not transcript.strip():
        return True  # nothing recognized this turn; keep listening
    reply = engine.respond(transcript)
    if reply:
        await speak(reply)
    return engine.stage.value != "ENDED"


async def _process_turn(websocket: WebSocket, speech_client, language: SupportedLanguage) -> str | None:
    """Runs one customer-utterance turn: opens a Google streaming session and
    feeds it 'media' frames as they arrive over the WebSocket, concurrently
    with waiting for that session to produce a transcript (Google signals
    end-of-utterance) or for a 'stop' event to end the call first.

    Returns the transcript (possibly "") once the turn completes, or None
    if the call ended (Twilio 'stop') before that happened.
    """
    session = speech_client.open_turn_session(language)
    transcript_task = asyncio.ensure_future(asyncio.to_thread(session.run))

    try:
        while not transcript_task.done():
            receive_task = asyncio.ensure_future(websocket.receive_text())
            done, pending = await asyncio.wait({transcript_task, receive_task}, return_when=asyncio.FIRST_COMPLETED)

            if receive_task not in done:
                receive_task.cancel()
                continue  # transcript_task finished; loop condition will exit

            raw = receive_task.result()
            message = parse_twilio_message(raw)
            event = message.get("event")
            if event == "media":
                session.feed(decode_media_payload(message))
            elif event == "stop":
                session.close()
                transcript_task.cancel()
                return None
    finally:
        session.close()

    return await transcript_task


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
        # Wait for Twilio's 'start' event before doing anything else -- it
        # carries the streamSid every outbound 'media' message must include.
        while state.stream_sid is None:
            raw = await websocket.receive_text()
            message = parse_twilio_message(raw)
            if message.get("event") == "start":
                state.stream_sid = message["start"]["streamSid"]
                state.call_sid = message["start"].get("callSid")
            elif message.get("event") == "stop":
                return

        greeting = engine.start()
        await _synthesize_and_send(websocket, speech_client, greeting, engine.language, state.stream_sid)

        async def speak(text: str) -> None:
            await _synthesize_and_send(websocket, speech_client, text, engine.language, state.stream_sid)

        while True:
            transcript = await _process_turn(websocket, speech_client, engine.language)
            should_continue = await handle_turn_result(engine, transcript, speak)
            if not should_continue:
                break

    except WebSocketDisconnect:
        logger.info("Twilio media stream disconnected for attempt=%s", attempt)
    finally:
        pop_conversation(attempt)
