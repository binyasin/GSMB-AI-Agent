"""Google Cloud Speech-to-Text / Text-to-Speech wrappers.

Chosen over Twilio's built-in `<Say>`/`<Gather>` speech because Twilio's TTS
(Amazon Polly) has no Urdu voice and its default speech recognition has weak
Urdu coverage — Google Cloud Speech supports ur-PK for both directions. See
the plan/README for the full rationale.

Audio format: 8kHz mu-law (`MULAW`), matching Twilio Media Streams' wire
format exactly, so no resampling is needed in either direction.
"""

from __future__ import annotations

import json
import logging
import queue

from app.config import Settings, get_settings
from app.schemas import SupportedLanguage

logger = logging.getLogger("calls")

_LANGUAGE_CODES = {
    SupportedLanguage.URDU: "ur-PK",
    SupportedLanguage.ENGLISH: "en-US",
}

# Google Cloud Text-to-Speech has no ur-PK voices at all (confirmed via
# list_voices(), 2026-08-08) -- only ur-IN. STT recognition works fine with
# ur-PK (real captured-call audio transcribed correctly), so only the TTS
# side needs a different language/voice; STT keeps using _LANGUAGE_CODES.
# Chirp3-HD is Google's newest, most natural-sounding tier -- picked after a
# live call using an unset (default-fallback) voice came out slow/unclear.
_TTS_VOICE = {
    SupportedLanguage.URDU: ("ur-IN", "ur-IN-Chirp3-HD-Aoede"),
    SupportedLanguage.ENGLISH: ("en-US", "en-US-Chirp3-HD-Aoede"),
}


def language_code_for(language: SupportedLanguage) -> str:
    return _LANGUAGE_CODES[language]


def _load_speech_credentials(settings: Settings):
    from google.oauth2.service_account import Credentials

    if settings.google_speech_credentials_json:
        info = json.loads(settings.google_speech_credentials_json)
        return Credentials.from_service_account_info(info)
    if settings.google_speech_credentials_file:
        return Credentials.from_service_account_file(settings.google_speech_credentials_file)
    if settings.google_service_account_json:
        info = json.loads(settings.google_service_account_json)
        return Credentials.from_service_account_info(info)
    if settings.google_service_account_file:
        return Credentials.from_service_account_file(settings.google_service_account_file)
    return None  # settings.require_speech() should have already caught this


class GoogleSpeechClient:
    """Thin wrapper around google-cloud-speech / google-cloud-texttospeech.

    Construction requires real Google Cloud Speech credentials
    (GOOGLE_SPEECH_CREDENTIALS_JSON/FILE, or a Google service account with
    Speech API roles) — see README "Speech setup". Not exercised by the
    automated test suite since no such credential exists yet in this
    environment; the STT/TTS request-building helpers below are.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.settings.require_speech()

        from google.cloud import speech, texttospeech

        credentials = _load_speech_credentials(self.settings)
        self._speech = speech.SpeechClient(credentials=credentials)
        self._tts = texttospeech.TextToSpeechClient(credentials=credentials)

    def streaming_config(self, language: SupportedLanguage):
        from google.cloud import speech

        return speech.StreamingRecognitionConfig(
            config=speech.RecognitionConfig(
                encoding=speech.RecognitionConfig.AudioEncoding.MULAW,
                sample_rate_hertz=8000,
                language_code=language_code_for(language),
                enable_automatic_punctuation=True,
            ),
            # Interim (non-final) results let StreamingTurnSession track a
            # "best transcript so far" that a caller can fall back to if the
            # turn hits its outer timeout before a natural end-of-utterance
            # -- otherwise a long, continuous reply with no clear pause gets
            # discarded entirely (confirmed on a live call, 2026-08-08).
            interim_results=True,
            # Without this, Google never emits END_OF_SINGLE_UTTERANCE, so
            # StreamingTurnSession.run()'s `for response in responses:` loop
            # has nothing to break on -- it just blocks past the point the
            # caller stops talking until the whole turn hits its outer
            # timeout (see media_stream.py:STT_TURN_TIMEOUT_SECONDS).
            single_utterance=True,
        )

    def transcribe_stream(self, audio_chunks, language: SupportedLanguage) -> str:
        """Blocking streaming-recognize call. `audio_chunks` is an iterable of raw
        mu-law byte chunks for one customer utterance (silence-delimited by the caller)."""
        from google.cloud import speech

        config = self.streaming_config(language)
        requests = (speech.StreamingRecognizeRequest(audio_content=chunk) for chunk in audio_chunks)
        responses = self._speech.streaming_recognize(config=config, requests=requests)
        transcript_parts = []
        for response in responses:
            for result in response.results:
                if result.is_final:
                    transcript_parts.append(result.alternatives[0].transcript)
        return " ".join(transcript_parts).strip()

    def open_turn_session(self, language: SupportedLanguage) -> "StreamingTurnSession":
        """One customer utterance's worth of streaming recognition, fed
        incrementally as Twilio Media Streams frames arrive (see
        app/webhooks/media_stream.py). Uses single_utterance=True so
        Google's own endpointing -- not a hand-rolled VAD -- decides when
        the customer has stopped talking."""
        return StreamingTurnSession(self._speech, self.streaming_config(language))

    def synthesize(self, text: str, language: SupportedLanguage) -> bytes:
        """Returns raw 8kHz mu-law audio bytes ready to stream back to Twilio."""
        from google.cloud import texttospeech

        tts_language_code, voice_name = _TTS_VOICE[language]
        response = self._tts.synthesize_speech(
            input=texttospeech.SynthesisInput(text=text),
            voice=texttospeech.VoiceSelectionParams(
                language_code=tts_language_code,
                name=voice_name,
            ),
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MULAW,
                sample_rate_hertz=8000,
            ),
        )
        return response.audio_content


class StreamingTurnSession:
    """Bridges an async producer (Twilio Media Streams frames, arriving one
    at a time over a WebSocket) with Google's blocking `streaming_recognize`
    generator, for exactly one customer utterance.

    Usage: `feed()` is called from the async side as audio frames arrive;
    `run()` is blocking (call it via `asyncio.to_thread`) and returns once
    Google signals end-of-utterance (or `close()` is called externally,
    e.g. because the call ended) -- whichever comes first.
    """

    def __init__(self, speech_client, streaming_config):
        self._speech_client = speech_client
        self._config = streaming_config
        self._queue: queue.Queue = queue.Queue()
        self._closed = False
        # Best transcript seen so far (interim or final), updated as results
        # arrive. Readable by a caller that gives up on run() ever returning
        # naturally (see media_stream.py's per-turn timeout) so a caller who
        # talks continuously without a long enough pause for Google's
        # endpointer to fire still gets *something* captured instead of the
        # whole turn being silently discarded (confirmed happening on a live
        # call, 2026-08-08: 30+ seconds of continuous real speech, zero
        # transcript, because interim_results was off).
        self.latest_transcript = ""

    def feed(self, chunk: bytes) -> None:
        if not self._closed:
            self._queue.put(chunk)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._queue.put(None)  # sentinel: stop the request generator

    def _requests(self):
        from google.cloud import speech

        while True:
            chunk = self._queue.get()
            if chunk is None:
                return
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

    def run(self) -> str:
        """Blocking. Returns the final transcript for this utterance ("" if
        nothing was recognized before the session ended).

        Google sends END_OF_SINGLE_UTTERANCE as a boundary marker on its own
        response, with zero results -- the response actually carrying the
        final transcript for that utterance arrives *after* it, on the very
        next message. Breaking as soon as the marker was seen (the previous
        behavior) discarded that trailing transcript every time, which is
        why every live call went completely silent after the greeting
        despite real, clearly-spoken audio reaching Google (confirmed via
        offline replay of captured call audio 2026-08-08) -- fixed by
        consuming exactly one more response after the marker before
        stopping."""
        from google.cloud import speech

        transcript = ""
        seen_end_of_utterance = False
        try:
            responses = self._speech_client.streaming_recognize(config=self._config, requests=self._requests())
            for response in responses:
                for result in response.results:
                    if result.alternatives:
                        self.latest_transcript = result.alternatives[0].transcript
                        if result.is_final:
                            transcript = result.alternatives[0].transcript
                if seen_end_of_utterance:
                    break
                if response.speech_event_type == speech.StreamingRecognizeResponse.SpeechEventType.END_OF_SINGLE_UTTERANCE:
                    seen_end_of_utterance = True
        finally:
            # Guarantee the request generator can terminate even if we broke
            # out early or an exception interrupted iteration -- otherwise
            # the generator (and the thread running this method) could hang
            # forever waiting on a queue nobody will ever close.
            self.close()
        return transcript.strip()
