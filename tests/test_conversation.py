from __future__ import annotations

import datetime as dt
import time
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


def test_dues_line_urdu_uses_lakh_hazar_sau_grouping():
    """Regression test: a live call (2026-08-08) confirmed a plain
    comma-grouped figure ("Rs. 608,311") read aloud by Urdu TTS comes out as
    disconnected digit groups ("608 rupay 311"), not an intelligible number.
    Urdu must use South Asian lakh/hazar/sau place-value grouping instead."""
    consumer = _consumer(outstanding_amount=608311, due_date=None)
    line = dues_line(consumer, SupportedLanguage.URDU)
    assert "6 lakh 8 hazar 3 sau 11 rupay" in line
    assert "608,311" not in line


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
def test_verified_customer_hears_dues_but_not_scheme_proactively():
    """Spec 2026-08-09 Dues & Installment Logic: the scheme is never
    mentioned upfront -- only dues, then the payment question. It's only
    offered later if the customer says they can't pay (see
    test_needs_more_time_offers_installment_then_captures_payment_contact)."""
    consumer = _consumer()

    def fake_classifier(stage, consumer, utterance, history):
        return CallDecision(intent=CustomerIntent.OTHER, verification_passed=True)

    engine = ConversationEngine(consumer, language=SupportedLanguage.ENGLISH, classifier=fake_classifier)
    engine.start()
    line = engine.respond("Yes speaking")
    assert "12,500" in line
    assert "installment" not in line.lower()
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
    assert "when was the payment made" in line.lower()
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


# ---------------------------------------------------------------------------
# "NEVER disconnect immediately after a single response" (K-Electric calling
# spec, user-supplied 2026-08-08): most categories must ask a follow-up
# question and LISTEN before closing, not end the call after one exchange.
# ---------------------------------------------------------------------------
def _classifier_for(main_response_decision: CallDecision):
    def fake_classifier(stage, consumer, utterance, history):
        if stage == ClassificationStage.VERIFY_IDENTITY:
            return CallDecision(intent=CustomerIntent.OTHER, verification_passed=True)
        return main_response_decision

    return fake_classifier


@pytest.mark.parametrize(
    "intent,expected_phrase",
    [
        (CustomerIntent.ALREADY_PAID, "receipt"),
        (CustomerIntent.DISPUTE, "anything else"),
        (CustomerIntent.HUMAN_ASSISTANCE, "anything else"),
        (CustomerIntent.INSTALLMENT_REQUEST, "anything else"),
        (CustomerIntent.REFUSES_TO_PAY, "reviewed"),
        (CustomerIntent.NOT_MY_ACCOUNT, "verify"),
        (CustomerIntent.COMPLAINT_NOT_ADDRESSED, "reference"),
        # NOT_MY_ADDRESS deliberately absent: it has its own dedicated
        # confirm-then-escalate flow, see the ADDRESS_CONFIRMATION tests below.
    ],
)
def test_followup_intents_ask_a_question_and_do_not_end_the_call(intent, expected_phrase):
    consumer = _consumer()
    engine = ConversationEngine(
        consumer, language=SupportedLanguage.ENGLISH,
        classifier=_classifier_for(CallDecision(intent=intent, human_followup=True)),
    )
    engine.start()
    engine.respond("Yes speaking")
    line = engine.respond("some response")

    assert engine.stage == ConversationStage.AWAITING_FOLLOWUP
    assert expected_phrase in line.lower()


# ---------------------------------------------------------------------------
# NOT_MY_ADDRESS: confirm-then-escalate flow (spec 2026-08-09 Address Rule)
# ---------------------------------------------------------------------------
def _classifier_sequence(*decisions):
    """Returns a fake classifier yielding each decision in order, one per
    call (VERIFY_IDENTITY always first). Lets a test drive a multi-stage
    conversation without hand-writing an if/elif per stage."""
    remaining = list(decisions)

    def classifier(stage, consumer, utterance, history):
        return remaining.pop(0)

    return classifier


def test_address_denial_asks_to_confirm_before_escalating():
    """The very first denial must never jump straight to asking for someone
    else's contact -- it reads back the address on record and asks the
    customer to confirm, in case the claim was hasty or mis-transcribed."""
    consumer = _consumer(address="House 12, Street 4, Karachi")
    engine = ConversationEngine(
        consumer, language=SupportedLanguage.ENGLISH,
        classifier=_classifier_sequence(
            CallDecision(intent=CustomerIntent.OTHER, verification_passed=True),
            CallDecision(intent=CustomerIntent.NOT_MY_ADDRESS, human_followup=True),
        ),
    )
    engine.start()
    engine.respond("Yes speaking")
    line = engine.respond("This isn't my house")

    assert engine.stage == ConversationStage.AWAITING_ADDRESS_CONFIRMATION
    assert "House 12, Street 4, Karachi" in line


def test_address_confirmed_resumes_normal_conversation():
    """A mis-transcription or actual confirmation at this stage must never
    be treated as a second denial -- it resumes the normal flow instead of
    escalating."""
    consumer = _consumer()
    engine = ConversationEngine(
        consumer, language=SupportedLanguage.ENGLISH,
        classifier=_classifier_sequence(
            CallDecision(intent=CustomerIntent.OTHER, verification_passed=True),
            CallDecision(intent=CustomerIntent.NOT_MY_ADDRESS, human_followup=True),
            CallDecision(intent=CustomerIntent.OTHER),  # confirms it IS their address
        ),
    )
    engine.start()
    engine.respond("Yes speaking")
    engine.respond("This isn't my house")
    line = engine.respond("Oh wait, actually yes it is")

    assert engine.stage == ConversationStage.AWAITING_MAIN_RESPONSE
    assert engine.decision.intent != CustomerIntent.NOT_MY_ADDRESS


def test_address_still_denied_asks_for_alternate_contact_then_saves_it():
    consumer = _consumer()
    engine = ConversationEngine(
        consumer, language=SupportedLanguage.ENGLISH,
        classifier=_classifier_sequence(
            CallDecision(intent=CustomerIntent.OTHER, verification_passed=True),
            CallDecision(intent=CustomerIntent.NOT_MY_ADDRESS, human_followup=True),
            CallDecision(intent=CustomerIntent.NOT_MY_ADDRESS, human_followup=True),  # still denies
        ),
    )
    engine.start()
    engine.respond("Yes speaking")
    engine.respond("This isn't my house")
    line = engine.respond("No, definitely not, wrong address")

    assert engine.stage == ConversationStage.AWAITING_ALTERNATE_CONTACT
    assert "contact" in line.lower() or "owner" in line.lower()

    line2 = engine.respond("You can reach the owner at 0300-1234567")
    assert engine.stage == ConversationStage.ENDED
    assert engine.decision.alternate_owner_contact is not None
    assert "note" in line2.lower() or "thank" in line2.lower()


def test_alternate_contact_not_given_still_ends_gracefully():
    consumer = _consumer()
    engine = ConversationEngine(
        consumer, language=SupportedLanguage.ENGLISH,
        classifier=_classifier_sequence(
            CallDecision(intent=CustomerIntent.OTHER, verification_passed=True),
            CallDecision(intent=CustomerIntent.NOT_MY_ADDRESS, human_followup=True),
            CallDecision(intent=CustomerIntent.NOT_MY_ADDRESS, human_followup=True),
        ),
    )
    engine.start()
    engine.respond("Yes speaking")
    engine.respond("This isn't my house")
    engine.respond("Not sure, wrong address")
    line = engine.respond("I don't have their number")

    assert engine.stage == ConversationStage.ENDED
    assert engine.decision.alternate_owner_contact is None


# ---------------------------------------------------------------------------
# NEEDS_MORE_TIME: installment offered only after "can't pay" (spec
# 2026-08-09 Dues & Installment Logic)
# ---------------------------------------------------------------------------
def test_installment_only_offered_after_cannot_pay():
    consumer = _consumer()
    engine = ConversationEngine(
        consumer, language=SupportedLanguage.ENGLISH,
        classifier=_classifier_sequence(
            CallDecision(intent=CustomerIntent.OTHER, verification_passed=True),
            CallDecision(intent=CustomerIntent.NEEDS_MORE_TIME, human_followup=True),
        ),
    )
    engine.start()
    engine.respond("Yes speaking")
    line = engine.respond("I don't have the money right now")

    assert engine.stage == ConversationStage.AWAITING_INSTALLMENT_INTEREST
    assert "installment" in line.lower() or "scheme" in line.lower()


def test_installment_accepted_asks_for_payment_contact_then_saves_it():
    consumer = _consumer(mobile_number="+923001234567")
    engine = ConversationEngine(
        consumer, language=SupportedLanguage.ENGLISH,
        classifier=_classifier_sequence(
            CallDecision(intent=CustomerIntent.OTHER, verification_passed=True),
            CallDecision(intent=CustomerIntent.NEEDS_MORE_TIME, human_followup=True),
            CallDecision(intent=CustomerIntent.INSTALLMENT_REQUEST, human_followup=True),
        ),
    )
    engine.start()
    engine.respond("Yes speaking")
    engine.respond("I can't pay right now")
    line = engine.respond("Yes, I'm interested in installments")

    assert engine.stage == ConversationStage.AWAITING_PAYMENT_CONTACT
    assert "+923001234567" in line

    line2 = engine.respond("Yes, that number is fine")
    assert engine.stage == ConversationStage.ENDED
    assert engine.decision.payment_contact_number == "+923001234567"
    assert "PDF" in line2 or "pdf" in line2.lower()


def test_installment_declined_closes_politely():
    consumer = _consumer()
    engine = ConversationEngine(
        consumer, language=SupportedLanguage.ENGLISH,
        classifier=_classifier_sequence(
            CallDecision(intent=CustomerIntent.OTHER, verification_passed=True),
            CallDecision(intent=CustomerIntent.NEEDS_MORE_TIME, human_followup=True),
            CallDecision(intent=CustomerIntent.REFUSES_TO_PAY, human_followup=True),
        ),
    )
    engine.start()
    engine.respond("Yes speaking")
    engine.respond("I can't pay right now")
    line = engine.respond("No, not interested in that")

    assert engine.stage == ConversationStage.ENDED
    assert "thank you" in line.lower() or "goodbye" in line.lower()


def test_needs_more_time_with_no_scheme_available_says_so_honestly():
    """Spec 2026-08-09 (human-like calling agent): if the customer can't pay
    but the consumer record has no scheme (installment_eligible=False),
    never invent one -- say so honestly and close, rather than falling
    through to a bare generic goodbye with no explanation."""
    consumer = _consumer(installment_eligible=False)
    engine = ConversationEngine(
        consumer, language=SupportedLanguage.ENGLISH,
        classifier=_classifier_sequence(
            CallDecision(intent=CustomerIntent.OTHER, verification_passed=True),
            CallDecision(intent=CustomerIntent.NEEDS_MORE_TIME, human_followup=True),
        ),
    )
    engine.start()
    engine.respond("Yes speaking")
    line = engine.respond("I can't pay right now")

    assert engine.stage == ConversationStage.ENDED
    assert "don't currently have confirmed installment information" in line.lower()
    assert "ke customer service centre" in line.lower()


# ---------------------------------------------------------------------------
# AI identity honesty (spec 2026-08-09): never pretend to be human if asked
# ---------------------------------------------------------------------------
def test_asking_if_ai_gets_an_honest_answer_not_the_generic_deflection():
    consumer = _consumer()
    engine = ConversationEngine(
        consumer, language=SupportedLanguage.ENGLISH,
        classifier=_classifier_for(CallDecision(intent=CustomerIntent.CUSTOMER_QUESTION, human_followup=True)),
    )
    engine.start()
    engine.respond("Yes speaking")
    line = engine.respond("Wait, are you an AI?")

    assert "ai assistant" in line.lower()
    assert "customer service" not in line.lower()


def test_unrelated_customer_question_still_gets_generic_deflection():
    consumer = _consumer()
    engine = ConversationEngine(
        consumer, language=SupportedLanguage.ENGLISH,
        classifier=_classifier_for(CallDecision(intent=CustomerIntent.CUSTOMER_QUESTION, human_followup=True)),
    )
    engine.start()
    engine.respond("Yes speaking")
    line = engine.respond("Why is my bill so high this month?")

    assert "customer service" in line.lower()
    assert "ai assistant" not in line.lower()


def test_followup_stage_closes_the_call_on_the_next_response():
    consumer = _consumer()
    engine = ConversationEngine(
        consumer, language=SupportedLanguage.ENGLISH,
        classifier=_classifier_for(CallDecision(intent=CustomerIntent.ALREADY_PAID, human_followup=True)),
    )
    engine.start()
    engine.respond("Yes speaking")
    engine.respond("I already paid")
    assert engine.stage == ConversationStage.AWAITING_FOLLOWUP

    line = engine.respond("Yes I have the receipt")
    assert engine.stage == ConversationStage.ENDED
    assert "goodbye" in line.lower() or "good day" in line.lower() or "thank you" in line.lower()


def test_followup_stage_do_not_call_still_ends_immediately():
    consumer = _consumer()
    call_count = 0

    def fake_classifier(stage, consumer, utterance, history):
        nonlocal call_count
        call_count += 1
        if stage == ClassificationStage.VERIFY_IDENTITY:
            return CallDecision(intent=CustomerIntent.OTHER, verification_passed=True)
        if call_count == 2:
            return CallDecision(intent=CustomerIntent.ALREADY_PAID, human_followup=True)
        return CallDecision(intent=CustomerIntent.OTHER, do_not_call=True)

    engine = ConversationEngine(consumer, language=SupportedLanguage.ENGLISH, classifier=fake_classifier)
    engine.start()
    engine.respond("Yes speaking")
    engine.respond("I already paid")
    assert engine.stage == ConversationStage.AWAITING_FOLLOWUP

    line = engine.respond("Stop calling me")
    assert engine.stage == ConversationStage.ENDED
    assert "not call" in line.lower()


def test_customer_question_answers_and_keeps_listening_instead_of_ending():
    """Spec Category H + end-of-call rule: never disconnect on a question."""
    consumer = _consumer()
    engine = ConversationEngine(
        consumer, language=SupportedLanguage.ENGLISH,
        classifier=_classifier_for(CallDecision(intent=CustomerIntent.CUSTOMER_QUESTION, human_followup=True)),
    )
    engine.start()
    engine.respond("Yes speaking")
    line = engine.respond("Why are you calling me?")

    assert engine.stage == ConversationStage.AWAITING_MAIN_RESPONSE  # still listening, not ended
    assert "customer service" in line.lower()


def test_secondary_intent_is_addressed_in_the_same_reply():
    consumer = _consumer()
    engine = ConversationEngine(
        consumer, language=SupportedLanguage.ENGLISH,
        classifier=_classifier_for(
            CallDecision(intent=CustomerIntent.ALREADY_PAID, secondary_intent=CustomerIntent.COMPLAINT_NOT_ADDRESSED, human_followup=True)
        ),
    )
    engine.start()
    engine.respond("Yes speaking")
    line = engine.respond("I already paid and my complaint was never resolved")

    assert "when was the payment made" in line.lower()  # primary (ALREADY_PAID) ack
    assert "understand your concern" in line.lower()  # secondary (COMPLAINT_NOT_ADDRESSED) ack


def test_customer_angry_prepends_empathetic_line():
    consumer = _consumer()
    engine = ConversationEngine(
        consumer, language=SupportedLanguage.ENGLISH,
        classifier=_classifier_for(CallDecision(intent=CustomerIntent.ALREADY_PAID, customer_angry=True, human_followup=True)),
    )
    engine.start()
    engine.respond("Yes speaking")
    line = engine.respond("I ALREADY paid this, why do you keep calling!")

    assert "understand that you are upset" in line.lower()
    assert "receipt" in line.lower()  # the actual category response still follows


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


def test_llm_classifier_with_fallback_uses_keyword_classifier_on_api_error(mocker):
    """Regression test: a live call (2026-08-08) confirmed classify_with_llm
    has no try/except of its own around the network call -- an Anthropic API
    error (that day: an account with zero credit balance) would otherwise
    propagate up and end the call mid-conversation instead of just degrading
    to the offline classifier for that turn."""
    from app.conversation_engine import _llm_classifier_with_fallback

    mocker.patch("app.conversation_engine.classify_with_llm", side_effect=RuntimeError("credit balance too low"))

    decision = _llm_classifier_with_fallback(ClassificationStage.MAIN_RESPONSE, _consumer(), "don't call me again", [])
    assert decision.intent == CustomerIntent.DO_NOT_CALL


# ---------------------------------------------------------------------------
# Multi-provider fallback chain (anthropic -> deepseek -> gemini -> keywords)
# ---------------------------------------------------------------------------
def test_fallback_chain_skips_unconfigured_providers(mocker):
    """Only anthropic has a key set -- deepseek/gemini must be skipped
    without even being called, not attempted and failed."""
    from app.conversation_engine import _llm_classifier_with_fallback

    settings = Settings(ai_api_key="fake-anthropic-key", llm_fallback_order="anthropic,deepseek,gemini")
    expected = CallDecision(intent=CustomerIntent.PROMISE_TO_PAY)
    mocker.patch("app.conversation_engine.classify_with_llm", return_value=expected)
    mock_deepseek = mocker.patch("app.conversation_engine._classify_with_deepseek")
    mock_gemini = mocker.patch("app.conversation_engine._classify_with_gemini")

    decision = _llm_classifier_with_fallback(
        ClassificationStage.MAIN_RESPONSE, _consumer(), "I will pay", [], settings=settings
    )
    assert decision is expected
    mock_deepseek.assert_not_called()
    mock_gemini.assert_not_called()


def test_fallback_chain_cascades_anthropic_to_deepseek_to_gemini(mocker):
    """All three configured; anthropic and deepseek both fail (e.g. the
    2026-08-08 zero-credit-balance case, plus a DeepSeek outage) -- gemini,
    last in the order, should still produce the classification."""
    from app.conversation_engine import _llm_classifier_with_fallback

    settings = Settings(
        ai_api_key="fake-anthropic-key",
        deepseek_api_key="fake-deepseek-key",
        gemini_api_key="fake-gemini-key",
        llm_fallback_order="anthropic,deepseek,gemini",
    )
    expected = CallDecision(intent=CustomerIntent.ALREADY_PAID)
    mocker.patch("app.conversation_engine.classify_with_llm", side_effect=RuntimeError("anthropic: zero credit balance"))
    mocker.patch("app.conversation_engine._classify_with_deepseek", side_effect=RuntimeError("deepseek: rate limited"))
    mock_gemini = mocker.patch("app.conversation_engine._classify_with_gemini", return_value=expected)

    decision = _llm_classifier_with_fallback(
        ClassificationStage.MAIN_RESPONSE, _consumer(), "I already paid this", [], settings=settings
    )
    assert decision is expected
    mock_gemini.assert_called_once()


def test_fallback_chain_falls_back_to_keywords_when_every_provider_fails(mocker):
    from app.conversation_engine import _llm_classifier_with_fallback

    settings = Settings(
        ai_api_key="fake-anthropic-key",
        deepseek_api_key="fake-deepseek-key",
        gemini_api_key="fake-gemini-key",
        llm_fallback_order="anthropic,deepseek,gemini",
    )
    mocker.patch("app.conversation_engine.classify_with_llm", side_effect=RuntimeError("down"))
    mocker.patch("app.conversation_engine._classify_with_deepseek", side_effect=RuntimeError("down"))
    mocker.patch("app.conversation_engine._classify_with_gemini", side_effect=RuntimeError("down"))

    decision = _llm_classifier_with_fallback(
        ClassificationStage.MAIN_RESPONSE, _consumer(), "don't call me again", [], settings=settings
    )
    assert decision.intent == CustomerIntent.DO_NOT_CALL  # keyword_fallback_classifier's own match


def test_fallback_chain_respects_custom_order(mocker):
    """LLM_FALLBACK_ORDER=gemini,anthropic should try gemini first even
    though anthropic is also configured."""
    from app.conversation_engine import _llm_classifier_with_fallback

    settings = Settings(
        ai_api_key="fake-anthropic-key",
        gemini_api_key="fake-gemini-key",
        llm_fallback_order="gemini,anthropic",
    )
    expected = CallDecision(intent=CustomerIntent.DISPUTE)
    mock_gemini = mocker.patch("app.conversation_engine._classify_with_gemini", return_value=expected)
    mock_anthropic = mocker.patch("app.conversation_engine.classify_with_llm")

    decision = _llm_classifier_with_fallback(
        ClassificationStage.MAIN_RESPONSE, _consumer(), "the bill is wrong", [], settings=settings
    )
    assert decision is expected
    mock_gemini.assert_called_once()
    mock_anthropic.assert_not_called()


def test_fallback_chain_skips_a_provider_still_in_cooldown_after_a_prior_failure(mocker):
    """Regression (2026-08-09): a live call confirmed real, measurable dead
    air -- with Anthropic and DeepSeek both out of credit, every single
    turn burned ~2-3s cascading through both failing HTTP calls before
    reaching Gemini. Once a provider fails once, it must be skipped
    (no HTTP call attempted at all) for the cooldown window instead of
    being retried on every subsequent turn."""
    from app.conversation_engine import _llm_classifier_with_fallback

    settings = Settings(
        ai_api_key="fake-anthropic-key",
        gemini_api_key="fake-gemini-key",
        llm_fallback_order="anthropic,gemini",
    )
    expected = CallDecision(intent=CustomerIntent.PROMISE_TO_PAY)
    mock_anthropic = mocker.patch("app.conversation_engine.classify_with_llm", side_effect=RuntimeError("no credit"))
    mock_gemini = mocker.patch("app.conversation_engine._classify_with_gemini", return_value=expected)

    # Turn 1: anthropic is tried, fails, gemini picks it up -- and anthropic
    # should now be in cooldown.
    _llm_classifier_with_fallback(ClassificationStage.MAIN_RESPONSE, _consumer(), "turn one", [], settings=settings)
    assert mock_anthropic.call_count == 1

    # Turn 2 (same call, moments later): anthropic must be skipped entirely
    # this time -- no second wasted HTTP round-trip.
    decision = _llm_classifier_with_fallback(
        ClassificationStage.MAIN_RESPONSE, _consumer(), "turn two", [], settings=settings
    )
    assert decision is expected
    assert mock_anthropic.call_count == 1  # NOT called again
    assert mock_gemini.call_count == 2


def test_provider_cooldown_expires_and_is_retried(mocker):
    from app.conversation_engine import _llm_classifier_with_fallback, _start_provider_cooldown

    settings = Settings(ai_api_key="fake-anthropic-key", llm_fallback_order="anthropic")
    expected = CallDecision(intent=CustomerIntent.OTHER)
    mock_anthropic = mocker.patch("app.conversation_engine.classify_with_llm", return_value=expected)

    _start_provider_cooldown("anthropic")
    mocker.patch("app.conversation_engine.time.monotonic", return_value=time.monotonic() + 999999)

    decision = _llm_classifier_with_fallback(
        ClassificationStage.MAIN_RESPONSE, _consumer(), "hello", [], settings=settings
    )
    assert decision is expected
    mock_anthropic.assert_called_once()


# ---------------------------------------------------------------------------
# _classify_with_deepseek / _classify_with_gemini: provider-specific plumbing
# ---------------------------------------------------------------------------
def test_classify_with_deepseek_requires_api_key():
    from app.conversation_engine import _classify_with_deepseek

    settings = Settings(deepseek_api_key=None)
    with pytest.raises(ConfigurationError):
        _classify_with_deepseek(ClassificationStage.MAIN_RESPONSE, _consumer(), "hello", [], settings=settings)


def test_classify_with_deepseek_parses_tool_call_response(mocker):
    from app.conversation_engine import _classify_with_deepseek

    settings = Settings(deepseek_api_key="fake-deepseek-key")
    fake_response = mocker.MagicMock()
    fake_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "classify_customer_response",
                                "arguments": '{"intent": "PROMISE_TO_PAY", "human_followup": false}',
                            }
                        }
                    ]
                }
            }
        ]
    }
    mock_post = mocker.patch("httpx.post", return_value=fake_response)

    decision = _classify_with_deepseek(
        ClassificationStage.MAIN_RESPONSE, _consumer(), "I will pay", [], settings=settings
    )
    assert decision.intent == CustomerIntent.PROMISE_TO_PAY
    fake_response.raise_for_status.assert_called_once()
    _, kwargs = mock_post.call_args
    assert kwargs["json"]["tool_choice"] == {"type": "function", "function": {"name": "classify_customer_response"}}


def test_classify_with_openrouter_requires_api_key():
    from app.conversation_engine import _classify_with_openrouter

    settings = Settings(openrouter_api_key=None)
    with pytest.raises(ConfigurationError):
        _classify_with_openrouter(ClassificationStage.MAIN_RESPONSE, _consumer(), "hello", [], settings=settings)


def test_classify_with_openrouter_parses_tool_call_response(mocker):
    from app.conversation_engine import _classify_with_openrouter

    settings = Settings(openrouter_api_key="fake-openrouter-key", openrouter_model="openai/gpt-4o-mini")
    fake_response = mocker.MagicMock()
    fake_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {"function": {"name": "classify_customer_response", "arguments": '{"intent": "DISPUTE"}'}}
                    ]
                }
            }
        ]
    }
    mock_post = mocker.patch("httpx.post", return_value=fake_response)

    decision = _classify_with_openrouter(
        ClassificationStage.MAIN_RESPONSE, _consumer(), "this bill is wrong", [], settings=settings
    )
    assert decision.intent == CustomerIntent.DISPUTE
    fake_response.raise_for_status.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "https://openrouter.ai/api/v1/chat/completions"
    assert kwargs["json"]["model"] == "openai/gpt-4o-mini"


def test_fallback_chain_reaches_openrouter_when_earlier_providers_fail(mocker):
    from app.conversation_engine import _llm_classifier_with_fallback

    settings = Settings(
        ai_api_key="fake-anthropic-key",
        deepseek_api_key="fake-deepseek-key",
        gemini_api_key="fake-gemini-key",
        openrouter_api_key="fake-openrouter-key",
        llm_fallback_order="anthropic,deepseek,gemini,openrouter",
    )
    expected = CallDecision(intent=CustomerIntent.NEEDS_MORE_TIME)
    mocker.patch("app.conversation_engine.classify_with_llm", side_effect=RuntimeError("down"))
    mocker.patch("app.conversation_engine._classify_with_deepseek", side_effect=RuntimeError("down"))
    mocker.patch("app.conversation_engine._classify_with_gemini", side_effect=RuntimeError("down"))
    mock_openrouter = mocker.patch("app.conversation_engine._classify_with_openrouter", return_value=expected)

    decision = _llm_classifier_with_fallback(
        ClassificationStage.MAIN_RESPONSE, _consumer(), "I can't pay right now", [], settings=settings
    )
    assert decision is expected
    mock_openrouter.assert_called_once()


def test_classify_with_gemini_requires_api_key():
    from app.conversation_engine import _classify_with_gemini

    settings = Settings(gemini_api_key=None)
    with pytest.raises(ConfigurationError):
        _classify_with_gemini(ClassificationStage.MAIN_RESPONSE, _consumer(), "hello", [], settings=settings)


def test_classify_with_gemini_parses_function_call_response(mocker):
    from app.conversation_engine import _classify_with_gemini

    settings = Settings(gemini_api_key="fake-gemini-key")
    fake_response = mocker.MagicMock()
    fake_response.json.return_value = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"functionCall": {"name": "classify_customer_response", "args": {"intent": "ALREADY_PAID"}}}
                    ]
                }
            }
        ]
    }
    mocker.patch("httpx.post", return_value=fake_response)

    decision = _classify_with_gemini(
        ClassificationStage.MAIN_RESPONSE, _consumer(), "I already paid", [], settings=settings
    )
    assert decision.intent == CustomerIntent.ALREADY_PAID
    fake_response.raise_for_status.assert_called_once()


def test_classify_with_gemini_falls_back_on_missing_function_call(mocker):
    from app.conversation_engine import _classify_with_gemini

    settings = Settings(gemini_api_key="fake-gemini-key")
    fake_response = mocker.MagicMock()
    fake_response.json.return_value = {"candidates": [{"content": {"parts": [{"text": "not a function call"}]}}]}
    mocker.patch("httpx.post", return_value=fake_response)

    decision = _classify_with_gemini(ClassificationStage.MAIN_RESPONSE, _consumer(), "huh?", [], settings=settings)
    assert decision.intent == CustomerIntent.OTHER
    assert decision.human_followup is True


# ---------------------------------------------------------------------------
# _to_gemini_schema: pydantic JSON Schema -> Gemini's OpenAPI-subset schema
# ---------------------------------------------------------------------------
def test_to_gemini_schema_inlines_refs_and_uppercases_types():
    from app.conversation_engine import _to_gemini_schema

    schema = _to_gemini_schema(CallDecision.model_json_schema())
    assert schema["type"] == "OBJECT"
    assert "$defs" not in schema
    assert "$ref" not in str(schema)

    intent_schema = schema["properties"]["intent"]
    assert intent_schema["type"] == "STRING"
    assert "PROMISE_TO_PAY" in intent_schema["enum"]

    # secondary_intent is `CustomerIntent | None` in pydantic -- should
    # collapse to the enum's schema plus nullable, not stay a two-branch union.
    secondary = schema["properties"]["secondary_intent"]
    assert secondary["type"] == "STRING"
    assert secondary["nullable"] is True


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


@pytest.mark.parametrize(
    "utterance,expected_intent",
    [
        ("I don't pay this bill.", CustomerIntent.REFUSES_TO_PAY),
        ("I won't pay.", CustomerIntent.REFUSES_TO_PAY),
        ("This is not my account.", CustomerIntent.NOT_MY_ACCOUNT),
        ("That's someone else's meter.", CustomerIntent.NOT_MY_ACCOUNT),
        ("This is not my house.", CustomerIntent.NOT_MY_ADDRESS),
        ("I don't live there anymore.", CustomerIntent.NOT_MY_ADDRESS),
        ("My complaint has not been resolved.", CustomerIntent.COMPLAINT_NOT_ADDRESSED),
        ("I don't have money to pay right now.", CustomerIntent.NEEDS_MORE_TIME),
        ("Why are you calling me?", CustomerIntent.CUSTOMER_QUESTION),
        # Regression (2026-08-09): this fell through to the generic "pay"
        # substring match and was misread as PROMISE_TO_PAY -- the word
        # "pay" appears in "pay nahi kar sakta" too.
        ("mere paas itne paise nahi hain, main itna bill pay nahi kar sakta", CustomerIntent.NEEDS_MORE_TIME),
        ("ada nahi kar sakta abhi", CustomerIntent.NEEDS_MORE_TIME),
    ],
)
def test_keyword_fallback_detects_new_category_variants(utterance, expected_intent):
    decision = keyword_fallback_classifier(ClassificationStage.MAIN_RESPONSE, _consumer(), utterance, [])
    assert decision.intent == expected_intent
