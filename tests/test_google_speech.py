from __future__ import annotations

from types import SimpleNamespace

from google.cloud import speech

from app.schemas import SupportedLanguage
from app.speech.google_speech import GoogleSpeechClient, StreamingTurnSession, language_code_for


def test_language_code_mapping():
    assert language_code_for(SupportedLanguage.URDU) == "ur-PK"
    assert language_code_for(SupportedLanguage.ENGLISH) == "en-US"


def test_synthesize_uses_ur_in_voice_for_urdu(mocker):
    """Regression test: Google Cloud TTS has no ur-PK voices at all
    (confirmed via list_voices(), 2026-08-08) -- requesting one produced a
    slow, unclear fallback voice on a live call. TTS must use ur-IN with an
    explicit high-quality voice name; STT recognition (a separate code path)
    keeps using ur-PK, which transcribes real captured call audio correctly."""
    from google.cloud import texttospeech

    client = GoogleSpeechClient.__new__(GoogleSpeechClient)
    client._tts = mocker.MagicMock()
    client._tts.synthesize_speech.return_value = SimpleNamespace(audio_content=b"fake-audio")

    client.synthesize("hello", SupportedLanguage.URDU)

    _, kwargs = client._tts.synthesize_speech.call_args
    voice = kwargs["voice"]
    assert voice.language_code == "ur-IN"
    assert voice.name == "ur-IN-Chirp3-HD-Aoede"


def test_streaming_config_sets_single_utterance():
    """Regression test: a live call (2026-08-08) silently hung for a full
    turn timeout because single_utterance was missing here -- Google never
    emitted END_OF_SINGLE_UTTERANCE, so StreamingTurnSession.run() had
    nothing to break its response loop on and just blocked past the point
    the caller stopped talking. GoogleSpeechClient.__init__ needs real
    credentials, but streaming_config() doesn't touch any instance state,
    so it's safe to call on an uninitialized instance here."""
    client = GoogleSpeechClient.__new__(GoogleSpeechClient)
    config = client.streaming_config(SupportedLanguage.URDU)
    assert config.single_utterance is True


def _fake_config():
    return speech.StreamingRecognitionConfig(
        config=speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.MULAW,
            sample_rate_hertz=8000,
            language_code="ur-PK",
        ),
        interim_results=False,
    )


def _fake_result(text: str, is_final: bool = True):
    return SimpleNamespace(is_final=is_final, alternatives=[SimpleNamespace(transcript=text)])


def test_run_returns_final_transcript_before_end_of_utterance(mocker):
    responses = [
        SimpleNamespace(speech_event_type=speech.StreamingRecognizeResponse.SpeechEventType.SPEECH_EVENT_UNSPECIFIED, results=[_fake_result("hello", is_final=False)]),
        SimpleNamespace(speech_event_type=speech.StreamingRecognizeResponse.SpeechEventType.SPEECH_EVENT_UNSPECIFIED, results=[_fake_result("hello there")]),
        SimpleNamespace(speech_event_type=speech.StreamingRecognizeResponse.SpeechEventType.END_OF_SINGLE_UTTERANCE, results=[]),
    ]
    mock_client = mocker.MagicMock()
    mock_client.streaming_recognize.return_value = iter(responses)

    session = StreamingTurnSession(mock_client, _fake_config())
    transcript = session.run()

    assert transcript == "hello there"
    mock_client.streaming_recognize.assert_called_once()


def test_run_returns_transcript_that_arrives_after_end_of_utterance_marker(mocker):
    """Regression test: Google's real streaming API sends END_OF_SINGLE_UTTERANCE
    as its own response with zero results -- the final transcript for that
    utterance arrives on the *next* response, not before it (confirmed via
    offline replay of a real captured call, 2026-08-08). A version of this
    code that broke as soon as it saw the marker discarded that trailing
    transcript on every single turn, so every live call went silent after
    the greeting despite the caller speaking clearly the whole time."""
    responses = [
        SimpleNamespace(speech_event_type=speech.StreamingRecognizeResponse.SpeechEventType.END_OF_SINGLE_UTTERANCE, results=[]),
        SimpleNamespace(speech_event_type=speech.StreamingRecognizeResponse.SpeechEventType.SPEECH_EVENT_UNSPECIFIED, results=[_fake_result("hello there")]),
    ]
    mock_client = mocker.MagicMock()
    mock_client.streaming_recognize.return_value = iter(responses)

    session = StreamingTurnSession(mock_client, _fake_config())
    transcript = session.run()

    assert transcript == "hello there"


def test_run_returns_empty_string_when_nothing_recognized(mocker):
    mock_client = mocker.MagicMock()
    mock_client.streaming_recognize.return_value = iter([
        SimpleNamespace(speech_event_type=speech.StreamingRecognizeResponse.SpeechEventType.END_OF_SINGLE_UTTERANCE, results=[]),
    ])

    session = StreamingTurnSession(mock_client, _fake_config())
    assert session.run() == ""


def test_run_closes_session_even_on_exception(mocker):
    mock_client = mocker.MagicMock()
    mock_client.streaming_recognize.side_effect = RuntimeError("network blip")

    session = StreamingTurnSession(mock_client, _fake_config())
    import pytest

    with pytest.raises(RuntimeError):
        session.run()
    assert session._closed is True


def test_feed_after_close_is_a_noop():
    session = StreamingTurnSession(SimpleNamespace(), _fake_config())
    session.close()
    session.feed(b"late-audio-should-be-dropped")
    # Only the close() sentinel should be in the queue -- the late feed was dropped.
    assert session._queue.qsize() == 1
    assert session._queue.get() is None


def test_requests_generator_yields_fed_chunks_then_stops():
    session = StreamingTurnSession(SimpleNamespace(), _fake_config())
    session.feed(b"chunk1")
    session.feed(b"chunk2")
    session.close()

    requests = list(session._requests())
    assert [r.audio_content for r in requests] == [b"chunk1", b"chunk2"]
