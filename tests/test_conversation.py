from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from app.config import ConfigurationError, Settings
from app.conversation_engine import (
    ClassificationStage,
    ConversationEngine,
    ConversationStage,
    _sanitize_decision,
    classify_with_llm,
    dues_line,
    keyword_fallback_classifier,
    scheme_line,
)
from app.schemas import CallDecision, ConsumerRecord, CustomerIntent, SupportedLanguage


def _consumer(**overrides) -> ConsumerRecord:
    base = dict(
        consumer_no="CN-001",
        consumer_name="Ali Raza",
        mobile_number="+923001234567",
        outstanding_amount=12500.0,
        due_date=dt.date(2026, 8, 15),
        installment_eligible=True,
        installment_details="3 installments of Rs. 4167",
    )
    base.update(overrides)
    return ConsumerRecord(**base)


# ---------------------------------------------------------------------------
# Templates never invent figures (spec Sec.2, Sec.22, Sec.24)
# ---------------------------------------------------------------------------
def test_dues_line_only_mentions_available_fields():
    consumer = _consumer(outstanding_amount=None, due_date=None)
    line = dues_line(consumer, SupportedLanguage.ENGLISH)
    assert "None" not in line
    assert "not available" in line


def test_dues_line_includes_all_present_fields():
    consumer = _consumer(outstanding_amount=12500, due_date=dt.date(2026, 8, 15))
    line = dues_line(consumer, SupportedLanguage.ENGLISH)
    assert "12,500" in line
    assert "15-08-2026" in line


def test_scheme_line_absent_when_not_eligible():
    consumer = _consumer(installment_eligible=False)
    assert scheme_line(consumer, SupportedLanguage.ENGLISH) is None


def test_scheme_line_never_invents_numbers_beyond_sheet_data():
    consumer = _consumer(installment_eligible=True, installment_details="3 installments of Rs. 4167")
    line = scheme_line(consumer, SupportedLanguage.ENGLISH)
    assert "3 installments of Rs. 4167" in line


# ---------------------------------------------------------------------------
# Full conversation flow (fake classifier — no live LLM)
# ---------------------------------------------------------------------------
def test_verified_customer_hears_dues_and_scheme():
    consumer = _consumer()

    def fake_classifier(stage, consumer, utterance, history):
        return CallDecision(intent=CustomerIntent.OTHER, verification_passed=True)

    engine = ConversationEngine(consumer, language=SupportedLanguage.ENGLISH, classifier=fake_classifier)
    engine.start()
    line = engine.respond("Yes speaking")
    assert "12,500" in line
    assert "installment" in line.lower()
    assert engine.stage == ConversationStage.AWAITING_MAIN_RESPONSE


def test_wrong_person_never_hears_dues():
    consumer = _consumer()

    def fake_classifier(stage, consumer, utterance, history):
        return CallDecision(intent=CustomerIntent.WRONG_PERSON, verification_passed=False)

    engine = ConversationEngine(consumer, language=SupportedLanguage.ENGLISH, classifier=fake_classifier)
    engine.start()
    line = engine.respond("Wrong number, sorry")
    assert "12,500" not in line
    assert engine.stage == ConversationStage.ENDED
    assert engine.decision.intent == CustomerIntent.VERIFICATION_FAILED


def test_do_not_call_ends_immediately_at_any_stage():
    consumer = _consumer()

    def fake_classifier(stage, consumer, utterance, history):
        return CallDecision(intent=CustomerIntent.DO_NOT_CALL, do_not_call=True)

    engine = ConversationEngine(consumer, language=SupportedLanguage.ENGLISH, classifier=fake_classifier)
    engine.start()
    line = engine.respond("Don't call me again")
    assert engine.stage == ConversationStage.ENDED
    assert engine.decision.do_not_call is True
    assert "not call" in line.lower()


def test_promise_to_pay_without_date_asks_followup_question_then_captures_it():
    consumer = _consumer()
    calls = []

    def fake_classifier(stage, consumer, utterance, history):
        calls.append(stage)
        if stage == ClassificationStage.VERIFY_IDENTITY:
            return CallDecision(intent=CustomerIntent.OTHER, verification_passed=True)
        if stage == ClassificationStage.MAIN_RESPONSE:
            return CallDecision(intent=CustomerIntent.PROMISE_TO_PAY, promise_to_pay_date=None)
        if stage == ClassificationStage.PROMISE_DATE:
            return CallDecision(intent=CustomerIntent.PROMISE_TO_PAY, promise_to_pay_date=dt.date(2026, 8, 20))
        raise AssertionError("unexpected stage")

    engine = ConversationEngine(consumer, language=SupportedLanguage.ENGLISH, classifier=fake_classifier)
    engine.start()
    engine.respond("Yes speaking")
    line2 = engine.respond("I will pay, just not sure when")
    assert "by when" in line2.lower()
    assert engine.stage == ConversationStage.AWAITING_PROMISE_DATE

    engine.respond("I'll pay by the 20th of August")
    assert engine.decision.promise_to_pay_date == dt.date(2026, 8, 20)
    assert engine.stage == ConversationStage.ENDED
    assert calls == [ClassificationStage.VERIFY_IDENTITY, ClassificationStage.MAIN_RESPONSE, ClassificationStage.PROMISE_DATE]


def test_already_paid_sets_human_followup_language():
    consumer = _consumer()

    def fake_classifier(stage, consumer, utterance, history):
        if stage == ClassificationStage.VERIFY_IDENTITY:
            return CallDecision(intent=CustomerIntent.OTHER, verification_passed=True)
        return CallDecision(intent=CustomerIntent.ALREADY_PAID, human_followup=True)

    engine = ConversationEngine(consumer, language=SupportedLanguage.ENGLISH, classifier=fake_classifier)
    engine.start()
    engine.respond("Yes speaking")
    line = engine.respond("I already paid this last week")
    assert "verify the payment record" in line.lower()
    assert engine.decision.human_followup is True


def test_dispute_routes_to_human_followup():
    consumer = _consumer()

    def fake_classifier(stage, consumer, utterance, history):
        if stage == ClassificationStage.VERIFY_IDENTITY:
            return CallDecision(intent=CustomerIntent.OTHER, verification_passed=True)
        return CallDecision(intent=CustomerIntent.DISPUTE, human_followup=True)

    engine = ConversationEngine(consumer, language=SupportedLanguage.ENGLISH, classifier=fake_classifier)
    engine.start()
    engine.respond("Yes speaking")
    engine.respond("This amount is wrong, I don't agree")
    assert engine.decision.intent == CustomerIntent.DISPUTE
    assert engine.decision.human_followup is True


def test_language_switch_mid_call():
    consumer = _consumer()
    engine = ConversationEngine(consumer, language=SupportedLanguage.URDU, classifier=lambda *a: CallDecision(intent=CustomerIntent.OTHER))
    assert "Assalam" in engine.start()
    engine.set_language(SupportedLanguage.ENGLISH)
    assert engine.language == SupportedLanguage.ENGLISH


def test_transcript_records_every_turn():
    consumer = _consumer()
    engine = ConversationEngine(consumer, classifier=lambda *a: CallDecision(intent=CustomerIntent.OTHER, verification_passed=True))
    engine.start()
    engine.respond("Ji han")
    speakers = [t.speaker for t in engine.transcript]
    assert speakers == ["Agent", "Customer", "Agent"]


# ---------------------------------------------------------------------------
# Guard against LLM fabricating a promise-to-pay date
# ---------------------------------------------------------------------------
def test_sanitize_decision_strips_unbacked_promise_date():
    decision = CallDecision(intent=CustomerIntent.NEEDS_MORE_TIME, promise_to_pay_date=dt.date(2026, 9, 1))
    sanitized = _sanitize_decision(decision, "I'll figure it out soon")
    assert sanitized.promise_to_pay_date is None


def test_sanitize_decision_keeps_date_backed_by_relative_word():
    decision = CallDecision(intent=CustomerIntent.WILL_PAY_TOMORROW, promise_to_pay_date=dt.date(2026, 8, 11))
    sanitized = _sanitize_decision(decision, "I'll pay tomorrow")
    assert sanitized.promise_to_pay_date == dt.date(2026, 8, 11)


# ---------------------------------------------------------------------------
# classify_with_llm: config gating + mocked Anthropic plumbing
# ---------------------------------------------------------------------------
def test_classify_with_llm_requires_ai_api_key():
    settings = Settings(ai_api_key=None)
    with pytest.raises(ConfigurationError):
        classify_with_llm(ClassificationStage.MAIN_RESPONSE, _consumer(), "hello", [], settings=settings)


def test_classify_with_llm_parses_tool_use_response(mocker):
    settings = Settings(ai_api_key="fake-key-for-mock-test", ai_model="claude-sonnet-5")

    fake_tool_block = SimpleNamespace(
        type="tool_use",
        input={"intent": "PROMISE_TO_PAY", "promise_to_pay_date": None, "human_followup": False, "do_not_call": False},
    )
    fake_message = SimpleNamespace(content=[fake_tool_block])

    mock_client = mocker.MagicMock()
    mock_client.messages.create.return_value = fake_message
    mocker.patch("anthropic.Anthropic", return_value=mock_client)

    decision = classify_with_llm(ClassificationStage.MAIN_RESPONSE, _consumer(), "I will pay", [], settings=settings)
    assert decision.intent == CustomerIntent.PROMISE_TO_PAY
    mock_client.messages.create.assert_called_once()
    _, kwargs = mock_client.messages.create.call_args
    assert kwargs["tool_choice"] == {"type": "tool", "name": "classify_customer_response"}


def test_classify_with_llm_falls_back_safely_on_invalid_payload(mocker):
    settings = Settings(ai_api_key="fake-key-for-mock-test")
    fake_tool_block = SimpleNamespace(type="tool_use", input={"intent": "NOT_A_REAL_INTENT"})
    fake_message = SimpleNamespace(content=[fake_tool_block])
    mock_client = mocker.MagicMock()
    mock_client.messages.create.return_value = fake_message
    mocker.patch("anthropic.Anthropic", return_value=mock_client)

    decision = classify_with_llm(ClassificationStage.MAIN_RESPONSE, _consumer(), "garbled", [], settings=settings)
    assert decision.intent == CustomerIntent.OTHER
    assert decision.human_followup is True


# ---------------------------------------------------------------------------
# keyword_fallback_classifier: natural Urdu phrasing coverage (regression
# tests for gaps found while demonstrating scenarios manually)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "utterance",
    [
        "Mujhe dobara kabhi call mat karna.",
        "Aage se call mat karo.",
        "Please call na karo mujhe.",
        "Do not call me again.",
    ],
)
def test_keyword_fallback_detects_do_not_call_variants(utterance):
    decision = keyword_fallback_classifier(ClassificationStage.MAIN_RESPONSE, _consumer(), utterance, [])
    assert decision.intent == CustomerIntent.DO_NOT_CALL
    assert decision.do_not_call is True


@pytest.mark.parametrize(
    "utterance",
    [
        "Ye bill ka amount galat hai, main is se agree nahi karta.",
        "Ye hisab galat hai.",
        "This amount is not correct.",
    ],
)
def test_keyword_fallback_detects_dispute_variants(utterance):
    decision = keyword_fallback_classifier(ClassificationStage.MAIN_RESPONSE, _consumer(), utterance, [])
    assert decision.intent == CustomerIntent.DISPUTE
    assert decision.human_followup is True


@pytest.mark.parametrize(
    "utterance",
    [
        "Mujhe is mein koi interest nahi hai.",
        "Mujhe dilchaspi nahi hai.",
        "I'm not interested.",
    ],
)
def test_keyword_fallback_detects_not_interested_variants(utterance):
    decision = keyword_fallback_classifier(ClassificationStage.MAIN_RESPONSE, _consumer(), utterance, [])
    assert decision.intent == CustomerIntent.NOT_INTERESTED
