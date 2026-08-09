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
from app.conversation_engine import ConversationEngine, no_speech_closing_line
from app.schemas import ConsumerRecord, SupportedLanguage

logger = logging.getLogger("calls")

router = APIRouter(prefix="/webhooks/voice", tags=["voice-webhooks"])

TTS_TIMEOUT_SECONDS = 10
STT_TURN_TIMEOUT_SECONDS = 30
MAX_CONSECUTIVE_EMPTY_TURNS = 3

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
    consecutive_empty_turns: int = 0,
) -> tuple[bool, int]:
    """Advance the conversation given one turn's transcript and speak the
    agent's reply if there is one.

    `transcript` is None when the call ended (Twilio 'stop') before this
    turn produced a transcript. Returns (should_continue, new_consecutive_
    empty_turns) -- should_continue is False if the call loop should stop
    (already ended, conversation reached ENDED, or MAX_CONSECUTIVE_EMPTY_TURNS
    unrecognized turns happened in a row).

    A live call (2026-08-08) originally confirmed that staying completely
    silent on every unrecognized turn made the call feel dead and got hung
    up on, so a re-prompt was added on every empty turn. A later live call
    (same day, after the actual STT recognition bug behind most of those
    empty turns was fixed) found hearing that same re-prompt line repeat
    made the call feel like a recording rather than a live conversation --
    explicit user feedback: stop re-prompting per turn. Kept: giving up
    gracefully with a closing line after MAX_CONSECUTIVE_EMPTY_TURNS in a
    row, so a call still can't loop in silence forever.
    """
    if transcript is None:
        return False, consecutive_empty_turns
    if not transcript.strip():
        consecutive_empty_turns += 1
        if consecutive_empty_turns >= MAX_CONSECUTIVE_EMPTY_TURNS:
            await speak(no_speech_closing_line(engine.language))
            return False, consecutive_empty_turns
        return True, consecutive_empty_turns
    reply = engine.respond(transcript)
    if reply:
        await speak(reply)
    return engine.stage.value != "ENDED", 0


async def _process_turn(
    websocket: WebSocket, speech_client, language: SupportedLanguage, timeout_seconds: float = STT_TURN_TIMEOUT_SECONDS
) -> str | None:
    """Runs one customer-utterance turn: opens a Google streaming session and
    feeds it 'media' frames as they arrive over the WebSocket, concurrently
    with waiting for that session to produce a transcript (Google signals
    end-of-utterance) or for a 'stop' event to end the call first.

    Returns the transcript (possibly "") once the turn completes, or None
    if the call ended (Twilio 'stop') before that happened.

    If no natural end-of-utterance shows up within timeout_seconds -- a
    caller who talks continuously with no pause long enough for Google's
    endpointer to fire, confirmed happening on a live call (2026-08-08:
    30+ seconds of real, clearly-spoken audio, zero transcript, because the
    turn was cut off before END_OF_SINGLE_UTTERANCE ever arrived) -- this
    falls back to session.latest_transcript (interim results) instead of
    discarding everything the caller said.
    """
    session = speech_client.open_turn_session(language)
    transcript_task = asyncio.ensure_future(asyncio.to_thread(session.run))
    chunks_fed = 0
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_seconds

    try:
        while not transcript_task.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning(
                    "turn exceeded %.0fs with no end-of-utterance; falling back to interim transcript %r",
                    timeout_seconds,
                    session.latest_transcript,
                )
                transcript_task.cancel()
                return session.latest_transcript

            receive_task = asyncio.ensure_future(websocket.receive_text())
            done, pending = await asyncio.wait({transcript_task, receive_task}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)

            if not done:
                receive_task.cancel()
                continue  # asyncio.wait's own timeout fired; loop re-checks remaining <= 0

            if receive_task not in done:
                receive_task.cancel()
                continue  # transcript_task finished; loop condition will exit

            raw = receive_task.result()
            message = parse_twilio_message(raw)
            event = message.get("event")
            if event == "media":
                session.feed(decode_media_payload(message))
                chunks_fed += 1
            elif event == "stop":
                session.close()
                transcript_task.cancel()
                return None
    finally:
        session.close()
        logger.info("turn fed %d inbound audio chunk(s) to Google STT", chunks_fed)

    return await transcript_task


@router.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    """Twilio Media Streams entry point.

    attempt_uid arrives as a <Parameter> inside Twilio's 'start' event
    (start.customParameters.attempt), NOT as a URL query string -- Twilio
    Media Streams rejects the WebSocket handshake outright (error 31920) if
    the <Stream> URL carries a query string, so it can't be a FastAPI query
    param here the way the HTTP webhooks below use it."""
    await websocket.accept()
    settings = get_settings()

    # Twilio sends 'connected' immediately on handshake, then 'start' right
    # after it -- 'connected' carries no stream/call metadata and must be
    # skipped rather than treated as the first real message.
    try:
        message: dict = {}
        while message.get("event") != "start":
            raw = await websocket.receive_text()
            message = parse_twilio_message(raw)
            if message.get("event") == "stop":
                return
            if message.get("event") not in ("connected", "start"):
                logger.error("expected Twilio 'connected'/'start' event, got %r; closing stream", message.get("event"))
                await websocket.close(code=1011)
                return
    except WebSocketDisconnect:
        return

    start = message["start"]
    attempt = start.get("customParameters", {}).get("attempt")
    state = MediaStreamState(attempt_uid=attempt, stream_sid=start["streamSid"], call_sid=start.get("callSid"))

    if not attempt:
        logger.error("Twilio 'start' event missing customParameters.attempt; closing stream")
        await websocket.close(code=1011)
        return

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
    logger.info("AI_SESSION_STARTED attempt=%s", attempt)

    # A hung Google API call here (TTS or STT) previously failed *silently*
    # forever -- no exception, no log line, just a dead call until Twilio's
    # own timeout kicked in -- because only WebSocketDisconnect was caught
    # and nothing bounded how long a single synthesize/recognize call could
    # block. TTS_TIMEOUT_SECONDS/STT_TURN_TIMEOUT_SECONDS convert a hang into
    # a loggable, recoverable failure instead.
    try:
        logger.info("attempt=%s: synthesizing greeting", attempt)
        greeting = engine.start()
        await asyncio.wait_for(
            _synthesize_and_send(websocket, speech_client, greeting, engine.language, state.stream_sid),
            timeout=TTS_TIMEOUT_SECONDS,
        )
        logger.info("AI_GREETING_SENT attempt=%s", attempt)

        async def speak(text: str) -> None:
            await asyncio.wait_for(
                _synthesize_and_send(websocket, speech_client, text, engine.language, state.stream_sid),
                timeout=TTS_TIMEOUT_SECONDS,
            )
            logger.info("AI_RESPONSE_SENT attempt=%s", attempt)

        turn_number = 0
        consecutive_empty_turns = 0
        while True:
            turn_number += 1
            logger.info("attempt=%s: turn %d: listening", attempt, turn_number)
            # _process_turn now enforces STT_TURN_TIMEOUT_SECONDS itself and
            # falls back to an interim transcript when it fires -- this
            # outer wait_for is just a defense-in-depth backstop with slack,
            # in case something inside it hangs past its own timeout logic.
            transcript = await asyncio.wait_for(
                _process_turn(websocket, speech_client, engine.language), timeout=STT_TURN_TIMEOUT_SECONDS + 10
            )
            if transcript:
                logger.info("CONSUMER_RESPONSE_RECEIVED attempt=%s turn=%d transcript=%r", attempt, turn_number, transcript)
            logger.info("attempt=%s: turn %d: transcript=%r", attempt, turn_number, transcript)
            should_continue, consecutive_empty_turns = await handle_turn_result(
                engine, transcript, speak, consecutive_empty_turns
            )
            if not should_continue:
                break

    except WebSocketDisconnect:
        logger.info("Twilio media stream disconnected for attempt=%s", attempt)
    except TimeoutError:
        logger.exception("attempt=%s: timed out waiting on Google Speech; ending call", attempt)
    except Exception:
        logger.exception("attempt=%s: unexpected error in media stream loop; ending call", attempt)
    # Deliberately NOT popping ACTIVE_CONVERSATIONS here. /webhooks/voice/status
    # is the only place that finalizes a call attempt (transcript + decision ->
    # DB + sheet), and it does its own pop_conversation() when Twilio's terminal
    # status webhook arrives. Popping here too used to race it: this handler's
    # `finally` always runs first (right as the AI hangs up, well before
    # Twilio's async status callback), so the status handler's pop_conversation()
    # always found the registry already emptied and silently fell back to an
    # empty-transcript/generic-OTHER decision -- every call's real transcript
    # and classified intent were being discarded, even on calls that completed
    # a full, correct conversation end-to-end.
