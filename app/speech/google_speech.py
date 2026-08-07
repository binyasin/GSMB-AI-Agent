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

from app.config import Settings, get_settings
from app.schemas import SupportedLanguage

logger = logging.getLogger("calls")

_LANGUAGE_CODES = {
    SupportedLanguage.URDU: "ur-PK",
    SupportedLanguage.ENGLISH: "en-US",
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
            interim_results=False,
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

    def synthesize(self, text: str, language: SupportedLanguage) -> bytes:
        """Returns raw 8kHz mu-law audio bytes ready to stream back to Twilio."""
        from google.cloud import texttospeech

        response = self._tts.synthesize_speech(
            input=texttospeech.SynthesisInput(text=text),
            voice=texttospeech.VoiceSelectionParams(
                language_code=language_code_for(language),
                ssml_gender=texttospeech.SsmlVoiceGender.FEMALE,
            ),
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MULAW,
                sample_rate_hertz=8000,
            ),
        )
        return response.audio_content
