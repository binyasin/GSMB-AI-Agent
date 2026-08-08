from __future__ import annotations

from types import SimpleNamespace

from google.cloud import speech

from app.schemas import SupportedLanguage
from app.speech.google_speech import StreamingTurnSession, language_code_for


def test_language_code_mapping():
    assert language_code_for(SupportedLanguage.URDU) == "ur-PK"
    assert language_code_for(SupportedLanguage.ENGLISH) == "en-US"


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
