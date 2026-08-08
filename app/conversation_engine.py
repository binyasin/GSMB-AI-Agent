"""AI conversation engine (spec Sec.19-32, Sec.39).

Design (confirmed with the user before building): a **deterministic
template state machine** speaks every amount/date/scheme detail, built
directly from `ConsumerRecord` — the LLM is never asked to produce those.
An LLM (Claude, via `classify_with_llm`) is used only to classify what the
*customer* said into the fixed `CustomerIntent` enum (spec Sec.25), detect
language, and extract an explicitly-stated promise-to-pay date. Its output
is always parsed into `CallDecision` (pydantic) before it can influence
anything, and `_sanitize_decision` refuses to keep a promise-to-pay date
that isn't backed by something date-like in the actual utterance — the
model is not trusted to "fill in" a date on its own.
"""

from __future__ import annotations

import datetime as dt
import logging
from enum import StrEnum

from app.config import get_settings
from app.schemas import CallDecision, ConsumerRecord, CustomerIntent, SupportedLanguage, TranscriptTurn

logger = logging.getLogger("calls")


# ---------------------------------------------------------------------------
# Deterministic scripted lines (spec Sec.20-32) — never touches the LLM
# ---------------------------------------------------------------------------
def _money(amount: float | None) -> str | None:
    if amount is None:
        return None
    return f"Rs. {amount:,.0f}"


def _money_urdu(amount: float | None) -> str | None:
    """South Asian lakh/hazar/sau place-value grouping, spoken in Urdu
    (e.g. 608311 -> "6 lakh 8 hazar 3 sau 11 rupay"). A live call
    (2026-08-08) confirmed a plain Western comma-grouped figure ("Rs.
    608,311") is unintelligible when read aloud by an Urdu TTS voice -- it
    comes out as disconnected digit groups ("608 rupay 311"), not a number.
    Each place-value chunk here stays small (at most 2 digits) so any TTS
    voice pronounces it correctly; only the lakh/hazar/sau words themselves
    need to be understood, not full Urdu number-words."""
    if amount is None:
        return None
    n = round(amount)
    if n == 0:
        return "0 rupay"
    parts = []
    crore, n = divmod(n, 10_000_000)
    if crore:
        parts.append(f"{crore} crore")
    lakh, n = divmod(n, 100_000)
    if lakh:
        parts.append(f"{lakh} lakh")
    hazar, n = divmod(n, 1_000)
    if hazar:
        parts.append(f"{hazar} hazar")
    sau, n = divmod(n, 100)
    if sau:
        parts.append(f"{sau} sau")
    if n:
        parts.append(str(n))
    return " ".join(parts) + " rupay"


def _date_str(d: dt.date | None) -> str | None:
    return d.strftime("%d-%m-%Y") if d else None


def greeting_line(consumer: ConsumerRecord, language: SupportedLanguage) -> str:
    name = consumer.consumer_name or "sahib/sahiba"
    if language == SupportedLanguage.URDU:
        return (
            "Assalam-o-Alaikum. Main GSM Brothers se call kar rahi hoon, "
            f"K-Electric consumer account ke hawale se. Kya main {name} se baat kar rahi hoon?"
        )
    return (
        "Hello, this is GSM Brothers calling on behalf of K-Electric regarding your consumer account. "
        f"Am I speaking with {name}?"
    )


def verification_failed_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return (
            "Maazrat, hum sirf authorized consumer se hi account ki tafseelat share kar sakte hain. "
            "Hamara numainda aap se dobara raabta karega. Allah Hafiz."
        )
    return (
        "I apologize, but we can only discuss account details with the authorized consumer. "
        "A representative will follow up separately. Thank you, goodbye."
    )


def dnc_ack_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return "Theek hai, hum aap ko dobara call nahin karenge. Shukriya."
    return "Understood — we will not call this number again. Thank you."


def dues_line(consumer: ConsumerRecord, language: SupportedLanguage) -> str:
    parts_ur = []
    parts_en = []
    outstanding_ur = _money_urdu(consumer.outstanding_amount)
    outstanding_en = _money(consumer.outstanding_amount)
    due = _date_str(consumer.due_date)
    if outstanding_ur:
        parts_ur.append(f"hamare authorized record ke mutabiq aap ke account par {outstanding_ur} outstanding hain")
        parts_en.append(f"according to our authorized record, your account has {outstanding_en} outstanding")
    if due:
        parts_ur.append(f"due date {due} hai")
        parts_en.append(f"with a due date of {due}")

    if language == SupportedLanguage.URDU:
        if not parts_ur:
            return "Maazrat, is waqt hamare record mein aap ke outstanding amount ki maloomat dastyab nahin hai."
        return "Ji, " + ", aur ".join(parts_ur) + "."
    if not parts_en:
        return "I'm sorry, the outstanding amount information is not available in our record right now."
    return "According to our records, " + ", and ".join(parts_en) + "."


def scheme_line(consumer: ConsumerRecord, language: SupportedLanguage) -> str | None:
    if not consumer.installment_eligible:
        return None
    details = consumer.installment_details or consumer.scheme_description
    if language == SupportedLanguage.URDU:
        if details:
            return (
                "Agar aap ek martaba poori amount pay nahin kar sakte to hamare available record ke mutabiq "
                f"aap ke account ke liye installment facility available hai: {details}."
            )
        return (
            "Agar aap ek martaba poori amount pay nahin kar sakte to hamare available record ke mutabiq "
            "aap ke account ke liye installment/payment facility available hai."
        )
    if details:
        return f"If you're unable to pay the full amount at once, an installment facility is available: {details}."
    return "If you're unable to pay the full amount at once, an installment/payment facility is available for your account."


def main_question_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return "Aap is bare mein kya soch rahe hain? Kya aap adaigi kar sakte hain?"
    return "How would you like to proceed with this payment?"


def promise_date_question_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return "Aap kab tak adaigi kar sakein ge?"
    return "By when do you expect to be able to pay?"


def already_paid_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return "Shukriya. Hamara numainda aap ki payment record verify kar lega."
    return "Thank you. Our representative can verify the payment record."


def already_paid_receipt_question_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return "Kya aap ke paas payment receipt ya transaction reference maujood hai?"
    return "Do you have the payment receipt or transaction reference available?"


def dispute_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return "Samajh gayi. Hum ye maamla hamare numainde ke through follow-up ke liye bhej rahe hain."
    return "Understood. We'll route this to a representative for follow-up."


def human_assistance_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return "Zaroor, hum aap ko hamare numainde se raabta karwane ki koshish karte hain."
    return "Of course — we'll arrange for a representative to assist you."


def installment_request_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return "Theek hai, hum aap ka installment request hamare numainde tak pohanchate hain jo aap se raabta karega."
    return "Understood — we'll forward your installment request to a representative who will follow up with you."


def refuses_to_pay_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return (
            "Samajh gayi. Balkeh, outstanding amount aap ke electricity account par dikha raha hai. "
            "Agar aap samajhte hain ke amount ghalat hai ya bill mein koi masla hai, to aap KE customer service "
            "ke through review karwa sakte hain. Agar koi dispute nahin hai to hum request karte hain ke "
            "outstanding amount clear kar dein takay aage koi masla na ho."
        )
    return (
        "I understand your concern. However, the outstanding amount is showing against the electricity account. "
        "If you believe the amount is incorrect or there is an issue with the bill, you may have it reviewed "
        "through the appropriate KE customer service channel. If there is no billing dispute, we kindly request "
        "you to clear the outstanding amount to avoid further issues."
    )


def refuses_to_pay_followup_question_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return "Kya aap billing issue review karwana chahenge, ya hum payment expect kar sakte hain?"
    return "Would you like to have the billing issue reviewed, or can we expect payment?"


def not_my_account_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return (
            "Takleef ke liye maazrat. Aisa lagta hai ke account ki maloomat verify honi chahiye. Agar ye account "
            "aap ka nahin hai to barah-e-karam koi payment na karein. Hum tajweez karte hain ke aap KE customer "
            "service se consumer/account details verify karwa lein takay record theek kiya ja sake agar zaroorat ho."
        )
    return (
        "I apologize for the inconvenience. It appears that the account information may need verification. "
        "Please do not make any payment if the account does not belong to you. We recommend verifying the "
        "consumer/account details with KE customer service so the record can be corrected if necessary."
    )


def not_my_account_followup_question_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return "Kya aap account details verify karwana chahenge?"
    return "Would you like to verify the account details?"


def not_my_address_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return (
            "Batane ka shukriya. Is soorat mein, account ki maloomat update ya verify honi chahiye. Barah-e-karam "
            "KE customer service se raabta karke occupancy ya address ki tabdeeli report karein aur account "
            "details check karwa lein."
        )
    return (
        "Thank you for informing me. In that case, the account information may need to be updated or verified. "
        "Please contact KE customer service to report the change of occupancy or address and have the account "
        "details checked."
    )


def anything_else_question_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return "Kya is account ke bare mein aap kuch aur batana chahenge?"
    return "Is there anything else regarding this account that you would like us to note?"


def complaint_not_addressed_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return (
            "Aap ka concern samajhti hoon, aur maazrat ke aap ki request abhi tak resolve nahin hui. "
            "Main note kar rahi/raha hoon ke request pending hai. Barah-e-karam apna complaint ya reference "
            "number available rakhein takay is maamle ko appropriate KE support channel ke through follow-up "
            "kiya ja sake."
        )
    return (
        "I understand your concern, and I apologize that your request has not yet been resolved. I will note "
        "your concern that the request is still pending. Please keep your complaint or reference number "
        "available so the matter can be followed up through the appropriate KE support channel."
    )


def complaint_reference_question_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return "Kya aap ke paas complaint ya request number maujood hai?"
    return "Do you have your complaint or request number?"


def customer_question_fallback_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return (
            "Aap ka sawal samajhti hoon. Main aap ko ghalat maloomat nahin dena chahti. "
            "Barah-e-karam tasdeeq aur mazeed madad ke liye KE customer service se raabta karein."
        )
    return (
        "I understand your question. I don't want to provide you with incorrect information. Please contact "
        "KE customer service for verification and further assistance."
    )


def angry_acknowledgment_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return "Main samajhti hoon ke aap pareshan hain, aur is takleef ke liye maazrat khwahish karti hoon."
    return "I understand that you are upset, and I apologize for the inconvenience."


def closing_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return "Waqt dene ka shukriya. Aap ke cooperation ka shukriya. Allah Hafiz."
    return "Thank you for your time and cooperation. Goodbye."


_CLOSING_BY_INTENT = {
    CustomerIntent.ALREADY_PAID: already_paid_line,
    CustomerIntent.DISPUTE: dispute_line,
    CustomerIntent.HUMAN_ASSISTANCE: human_assistance_line,
    CustomerIntent.INSTALLMENT_REQUEST: installment_request_line,
    CustomerIntent.REFUSES_TO_PAY: refuses_to_pay_line,
    CustomerIntent.NOT_MY_ACCOUNT: not_my_account_line,
    CustomerIntent.NOT_MY_ADDRESS: not_my_address_line,
    CustomerIntent.COMPLAINT_NOT_ADDRESSED: complaint_not_addressed_line,
}

# Intents that must ask a follow-up question and LISTEN before closing,
# instead of disconnecting right after the acknowledgment line (the core
# rule from the user's calling-behavior spec: "NEVER disconnect immediately
# after saying the consumer's name" / immediately after any single response).
_FOLLOWUP_QUESTION_BY_INTENT = {
    CustomerIntent.ALREADY_PAID: already_paid_receipt_question_line,
    CustomerIntent.DISPUTE: anything_else_question_line,
    CustomerIntent.HUMAN_ASSISTANCE: anything_else_question_line,
    CustomerIntent.INSTALLMENT_REQUEST: anything_else_question_line,
    CustomerIntent.REFUSES_TO_PAY: refuses_to_pay_followup_question_line,
    CustomerIntent.NOT_MY_ACCOUNT: not_my_account_followup_question_line,
    CustomerIntent.NOT_MY_ADDRESS: anything_else_question_line,
    CustomerIntent.COMPLAINT_NOT_ADDRESSED: complaint_reference_question_line,
}


def closing_line_for_intent(decision: CallDecision, language: SupportedLanguage) -> str:
    special = _CLOSING_BY_INTENT.get(decision.intent)
    prefix = special(language) + " " if special else ""
    return prefix + closing_line(language)


def secondary_intent_line(decision: CallDecision, language: SupportedLanguage) -> str | None:
    """Best-effort second-concern acknowledgment (LLM path only -- the
    offline keyword fallback never sets secondary_intent, since it's pattern
    matching on one utterance, not real multi-concern NLU)."""
    if decision.secondary_intent is None:
        return None
    special = _CLOSING_BY_INTENT.get(decision.secondary_intent)
    return special(language) if special else None


def no_speech_closing_line(language: SupportedLanguage) -> str:
    """Spoken when MAX_CONSECUTIVE_EMPTY_TURNS unrecognized turns happen in
    a row -- ends the call gracefully instead of looping re-prompts
    indefinitely."""
    if language == SupportedLanguage.URDU:
        return "Maazrat, hum aap ki awaz sun nahin pa rahe. Hum baad mein dobara raabta karein ge. Allah Hafiz."
    return "I'm sorry, we're unable to hear you clearly right now. We'll follow up again later. Goodbye."


# ---------------------------------------------------------------------------
# LLM-backed NLU classification (spec Sec.25, Sec.39)
# ---------------------------------------------------------------------------
class ClassificationStage(StrEnum):
    VERIFY_IDENTITY = "VERIFY_IDENTITY"
    MAIN_RESPONSE = "MAIN_RESPONSE"
    PROMISE_DATE = "PROMISE_DATE"
    FOLLOWUP = "FOLLOWUP"


_SYSTEM_PROMPT_TEMPLATE = """You are the NLU component of an AI recovery-calling agent for GSM Brothers, \
calling on behalf of K-Electric about an outstanding consumer bill. You are given ONE customer utterance \
(already transcribed) plus the conversation stage. Classify it using the classify_customer_response tool.

Understand meaning, not just exact words -- for example:
- "I've cleared it already" -> ALREADY_PAID
- "That's somebody else's meter" -> NOT_MY_ACCOUNT
- "I don't live there anymore" -> NOT_MY_ADDRESS
- "Nobody has solved my complaint" -> COMPLAINT_NOT_ADDRESSED
- "I'll arrange the money" -> PROMISE_TO_PAY
- "I can't afford it at the moment" -> NEEDS_MORE_TIME
- "Why are you calling me?" -> CUSTOMER_QUESTION
- "I don't pay" / "I won't pay this bill" -> REFUSES_TO_PAY (distinct from DISPUTE: REFUSES_TO_PAY is an outright
  refusal with no stated reason; DISPUTE is specifically "the amount/bill is wrong")

Rules:
- Do NOT invent, calculate, or assume any amount, date, or scheme detail. Only extract what the customer \
explicitly said.
- Only set promise_to_pay_date if the customer stated a specific date or an unambiguous relative date \
("tomorrow", "next Friday"). If they were vague ("soon", "jald hi"), leave promise_to_pay_date null and put \
their own words in `notes` instead.
- Set human_followup=true for: disputes, installment requests, "already paid" claims, requests for a human, \
NOT_MY_ACCOUNT, NOT_MY_ADDRESS, COMPLAINT_NOT_ADDRESSED, or anything you are not confident about.
- Set do_not_call=true only if the customer explicitly asked not to be called again.
- If the utterance raises a second, genuinely distinct concern alongside the primary one (e.g. "I already paid \
AND my complaint hasn't been resolved"), set secondary_intent to that second concern's category. Only use this \
for a real second concern, not a restatement of the same one.
- Set customer_angry=true if the tone is hostile, frustrated, or raised -- independent of which category the \
content falls into.
- Current conversation stage: {stage}
"""


def _build_anthropic_client(settings):
    settings.require_ai()
    from anthropic import Anthropic

    return Anthropic(api_key=settings.ai_api_key)


def _sanitize_decision(decision: CallDecision, utterance: str) -> CallDecision:
    """Defensive guard against a fabricated promise-to-pay date."""
    relative_intents = {
        CustomerIntent.WILL_PAY_TODAY,
        CustomerIntent.WILL_PAY_TOMORROW,
        CustomerIntent.WILL_PAY_THIS_WEEK,
    }
    if decision.promise_to_pay_date is not None:
        has_digit = any(ch.isdigit() for ch in utterance)
        has_relative_word = any(
            w in utterance.lower() for w in ("today", "tomorrow", "kal", "aaj", "hafte", "week", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        )
        if decision.intent not in relative_intents and not has_digit and not has_relative_word:
            decision = decision.model_copy(update={"promise_to_pay_date": None})
    return decision


def classify_with_llm(
    stage: ClassificationStage,
    consumer: ConsumerRecord,
    utterance: str,
    history: list[TranscriptTurn] | None = None,
    settings=None,
) -> CallDecision:
    """The single LLM entry point. Raises ConfigurationError if AI_API_KEY is unset."""
    settings = settings or get_settings()
    client = _build_anthropic_client(settings)
    history = history or []

    schema = CallDecision.model_json_schema()
    tool = {
        "name": "classify_customer_response",
        "description": "Structured classification of the customer's utterance.",
        "input_schema": schema,
    }
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(stage=stage.value)
    transcript_text = "\n".join(f"{t.speaker}: {t.message}" for t in history)

    message = client.messages.create(
        model=settings.ai_model,
        max_tokens=512,
        system=system_prompt,
        tools=[tool],
        tool_choice={"type": "tool", "name": "classify_customer_response"},
        messages=[
            {
                "role": "user",
                "content": f"Conversation so far:\n{transcript_text}\n\nLatest customer utterance: {utterance!r}",
            }
        ],
    )

    for block in message.content:
        if getattr(block, "type", None) == "tool_use":
            try:
                decision = CallDecision.model_validate(block.input)
            except Exception:
                logger.exception("LLM returned an invalid CallDecision payload: %r", block.input)
                return CallDecision(
                    intent=CustomerIntent.OTHER,
                    human_followup=True,
                    notes="LLM output failed schema validation; routed to human review.",
                )
            return _sanitize_decision(decision, utterance)

    logger.warning("LLM returned no tool_use block for stage=%s", stage.value)
    return CallDecision(
        intent=CustomerIntent.OTHER,
        human_followup=True,
        notes="LLM returned no classification; routed to human review.",
    )


def _llm_classifier_with_fallback(
    stage: ClassificationStage, consumer: ConsumerRecord, utterance: str, history: list[TranscriptTurn] | None = None
) -> CallDecision:
    """Wraps classify_with_llm so a live call keeps going even if the LLM
    call itself fails (API outage, rate limit, or -- the case that prompted
    this, 2026-08-08 -- an Anthropic account with an expired/zero credit
    balance): classify_with_llm has no try/except of its own around the
    network call, so an error there would otherwise propagate up and end
    the call mid-conversation. Falls back to the same offline classifier
    used when no AI_API_KEY is configured at all."""
    try:
        return classify_with_llm(stage, consumer, utterance, history)
    except Exception:
        logger.exception("classify_with_llm failed; falling back to keyword_fallback_classifier for this turn")
        return keyword_fallback_classifier(stage, consumer, utterance, history)


_DATE_HINT_WORDS = (
    "today", "aaj", "tomorrow", "kal", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday", "week", "hafte",
)


def keyword_fallback_classifier(
    stage: ClassificationStage, consumer: ConsumerRecord, utterance: str, history: list[TranscriptTurn] | None = None
) -> CallDecision:
    """Deterministic, offline keyword classifier used only when AI_API_KEY is not
    configured (TEST_MODE/DRY_RUN with no LLM credential yet). Not a substitute
    for classify_with_llm in production — see README "AI provider setup"."""
    text = utterance.lower()

    _dnc_phrases = ("don't call", "do not call", "stop calling", "mat karo call", "call mat karo", "call mat karna", "call na karo", "call band karo")
    if any(p in text for p in _dnc_phrases) or ("call" in text and any(p in text for p in ("mat karna", "mat karo", "na karo", "band karo"))):
        return CallDecision(intent=CustomerIntent.DO_NOT_CALL, do_not_call=True, human_followup=False)

    if stage == ClassificationStage.VERIFY_IDENTITY:
        if any(p in text for p in ("wrong number", "wrong person", "ghalat number", "koi aur")):
            return CallDecision(intent=CustomerIntent.WRONG_PERSON, verification_passed=False)
        if any(p in text for p in ("yes", "ji han", "haan", "speaking", "yeah")):
            return CallDecision(intent=CustomerIntent.OTHER, verification_passed=True)
        return CallDecision(intent=CustomerIntent.OTHER, verification_passed=True)

    if any(p in text for p in ("already paid", "maine pay kar", "pay kar diya")):
        return CallDecision(intent=CustomerIntent.ALREADY_PAID, human_followup=True)
    _not_my_account_phrases = ("not my account", "not my meter", "someone else's meter", "somebody else's meter", "mera account nahi", "ye mera meter nahi", "mera meter nahi")
    if any(p in text for p in _not_my_account_phrases):
        return CallDecision(intent=CustomerIntent.NOT_MY_ACCOUNT, human_followup=True)
    _not_my_address_phrases = ("not my house", "not my address", "don't live there", "dont live there", "i've moved", "i have moved", "mera ghar nahi", "yahan nahi rehta", "yahan nahi rehti")
    if any(p in text for p in _not_my_address_phrases):
        return CallDecision(intent=CustomerIntent.NOT_MY_ADDRESS, human_followup=True)
    _complaint_phrases = ("complaint", "not resolved", "not addressed", "shikayat", "meri complaint", "request pending")
    if any(p in text for p in _complaint_phrases):
        return CallDecision(intent=CustomerIntent.COMPLAINT_NOT_ADDRESSED, human_followup=True)
    _dispute_phrases = ("dispute", "wrong amount", "not correct", "galat bill", "sahi nahi", "galat hai")
    if any(p in text for p in _dispute_phrases) or ("galat" in text and any(p in text for p in ("bill", "amount", "hisab"))):
        return CallDecision(intent=CustomerIntent.DISPUTE, human_followup=True)
    _refuses_phrases = ("i don't pay", "i dont pay", "won't pay", "wont pay", "not going to pay", "will not pay", "nahi doon ga", "nahi dena", "nahi de sakta")
    if any(p in text for p in _refuses_phrases):
        return CallDecision(intent=CustomerIntent.REFUSES_TO_PAY, human_followup=True)
    if any(p in text for p in ("installment", "qist", "scheme")):
        return CallDecision(intent=CustomerIntent.INSTALLMENT_REQUEST, human_followup=True)
    if any(p in text for p in ("human", "representative", "agent", "insaan")):
        return CallDecision(intent=CustomerIntent.HUMAN_ASSISTANCE, human_followup=True)
    if any(p in text for p in ("not interested", "nahi karna", "dilchaspi nahi", "interest nahi")):
        return CallDecision(intent=CustomerIntent.NOT_INTERESTED)
    if any(p in text for p in ("call back", "callback", "baad mein call")):
        return CallDecision(intent=CustomerIntent.CALL_BACK, human_followup=True)

    _cannot_pay_phrases = ("can't pay", "cant pay", "cannot pay", "no money", "don't have money", "dont have money", "paisay nahi", "abhi nahi", "need time", "waqt chahiye")
    if any(p in text for p in _cannot_pay_phrases):
        return CallDecision(intent=CustomerIntent.NEEDS_MORE_TIME, human_followup=True, notes=utterance)

    if any(p in text for p in ("pay", "adaigi", "ada kar")):
        if stage == ClassificationStage.PROMISE_DATE:
            has_hint = any(w in text for w in _DATE_HINT_WORDS) or any(ch.isdigit() for ch in text)
            if not has_hint:
                return CallDecision(intent=CustomerIntent.PROMISE_TO_PAY, notes=utterance)
            if "today" in text or "aaj" in text:
                return CallDecision(intent=CustomerIntent.WILL_PAY_TODAY, promise_to_pay_date=dt.date.today())
            if "tomorrow" in text or "kal" in text:
                return CallDecision(
                    intent=CustomerIntent.WILL_PAY_TOMORROW,
                    promise_to_pay_date=dt.date.today() + dt.timedelta(days=1),
                )
            return CallDecision(intent=CustomerIntent.PROMISE_TO_PAY, notes=utterance)
        return CallDecision(intent=CustomerIntent.PROMISE_TO_PAY, promise_to_pay_date=None)

    _question_starters = ("why ", "what ", "how ", "kyun ", "kya ", "kaise ")
    if "?" in utterance or any(text.startswith(p) for p in _question_starters):
        return CallDecision(intent=CustomerIntent.CUSTOMER_QUESTION, human_followup=True, notes=utterance)

    return CallDecision(intent=CustomerIntent.OTHER, human_followup=True, notes=utterance)


# ---------------------------------------------------------------------------
# Conversation state machine
# ---------------------------------------------------------------------------
class ConversationStage(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    AWAITING_IDENTITY_REPLY = "AWAITING_IDENTITY_REPLY"
    AWAITING_MAIN_RESPONSE = "AWAITING_MAIN_RESPONSE"
    AWAITING_PROMISE_DATE = "AWAITING_PROMISE_DATE"
    AWAITING_FOLLOWUP = "AWAITING_FOLLOWUP"
    ENDED = "ENDED"


Classifier = "Callable[[ClassificationStage, ConsumerRecord, str, list[TranscriptTurn]], CallDecision]"


class ConversationEngine:
    """Drives one call's conversation. `classifier` is injectable so tests
    (and any code that hasn't configured AI_API_KEY) can supply a fake."""

    def __init__(
        self,
        consumer: ConsumerRecord,
        language: SupportedLanguage = SupportedLanguage.URDU,
        classifier=None,
    ):
        self.consumer = consumer
        self.language = language
        self.stage = ConversationStage.NOT_STARTED
        self.transcript: list[TranscriptTurn] = []
        self.decision = CallDecision(intent=CustomerIntent.OTHER)
        self._classifier = classifier or self._default_classifier()
        self._pending_followup_intent: CustomerIntent | None = None

    @staticmethod
    def _default_classifier():
        settings = get_settings()
        if settings.ai_api_key:
            return _llm_classifier_with_fallback
        logger.warning("AI_API_KEY not configured; using offline keyword_fallback_classifier (not for production)")
        return keyword_fallback_classifier

    def _log(self, speaker: str, message: str) -> None:
        self.transcript.append(TranscriptTurn(speaker=speaker, timestamp=dt.datetime.now(dt.timezone.utc), message=message))

    def set_language(self, language: SupportedLanguage) -> None:
        """Mid-call language switch (spec Sec.19: switch if the customer speaks English/asks for Urdu)."""
        self.language = language

    def start(self) -> str:
        line = greeting_line(self.consumer, self.language)
        self._log("Agent", line)
        self.stage = ConversationStage.AWAITING_IDENTITY_REPLY
        return line

    def respond(self, utterance: str) -> str:
        if self.stage in (ConversationStage.NOT_STARTED, ConversationStage.ENDED):
            return ""
        self._log("Customer", utterance)

        if self.stage == ConversationStage.AWAITING_IDENTITY_REPLY:
            line = self._handle_identity_reply(utterance)
        elif self.stage == ConversationStage.AWAITING_MAIN_RESPONSE:
            line = self._handle_main_response(utterance)
        elif self.stage == ConversationStage.AWAITING_PROMISE_DATE:
            line = self._handle_promise_date(utterance)
        elif self.stage == ConversationStage.AWAITING_FOLLOWUP:
            line = self._handle_followup(utterance)
        else:
            line = ""

        self._log("Agent", line)
        return line

    def _handle_identity_reply(self, utterance: str) -> str:
        decision = self._classifier(ClassificationStage.VERIFY_IDENTITY, self.consumer, utterance, self.transcript)
        self.decision = decision

        if decision.do_not_call:
            self.stage = ConversationStage.ENDED
            return dnc_ack_line(self.language)

        if decision.intent in (CustomerIntent.WRONG_PERSON, CustomerIntent.WRONG_NUMBER) or decision.verification_passed is False:
            self.stage = ConversationStage.ENDED
            self.decision = decision.model_copy(update={"intent": CustomerIntent.VERIFICATION_FAILED, "next_action": "END_CALL"})
            return verification_failed_line(self.language)

        # Verified (or classifier didn't explicitly fail it) -> inform dues + scheme.
        self.decision = decision.model_copy(update={"verification_passed": True})
        lines = [dues_line(self.consumer, self.language)]
        scheme = scheme_line(self.consumer, self.language)
        if scheme:
            lines.append(scheme)
        lines.append(main_question_line(self.language))
        self.stage = ConversationStage.AWAITING_MAIN_RESPONSE
        return " ".join(lines)

    def _apply_tone(self, line: str, decision: CallDecision) -> str:
        """Prepends an empathetic acknowledgment when the customer's tone
        was flagged as angry/frustrated -- independent of which category
        the content itself falls into (spec Category I: never argue, stay
        calm and respectful, regardless of the actual issue)."""
        if decision.customer_angry:
            return angry_acknowledgment_line(self.language) + " " + line
        return line

    def _handle_main_response(self, utterance: str) -> str:
        decision = self._classifier(ClassificationStage.MAIN_RESPONSE, self.consumer, utterance, self.transcript)
        decision = decision.model_copy(update={"verification_passed": self.decision.verification_passed})
        self.decision = decision

        if decision.do_not_call:
            self.stage = ConversationStage.ENDED
            return dnc_ack_line(self.language)

        # Never disconnect on a question -- answer (or say we can't) and keep
        # listening in the same stage, per spec Category H / the end-of-call
        # rule ("only disconnect once the question has been addressed").
        if decision.intent == CustomerIntent.CUSTOMER_QUESTION:
            parts = []
            secondary = secondary_intent_line(decision, self.language)
            if secondary:
                parts.append(secondary)
            parts.append(customer_question_fallback_line(self.language))
            return self._apply_tone(" ".join(parts), decision)

        if decision.intent == CustomerIntent.PROMISE_TO_PAY and decision.promise_to_pay_date is None:
            self.stage = ConversationStage.AWAITING_PROMISE_DATE
            return self._apply_tone(promise_date_question_line(self.language), decision)

        followup_question = _FOLLOWUP_QUESTION_BY_INTENT.get(decision.intent)
        if followup_question is not None:
            self._pending_followup_intent = decision.intent
            self.stage = ConversationStage.AWAITING_FOLLOWUP
            parts = []
            ack = _CLOSING_BY_INTENT.get(decision.intent)
            if ack:
                parts.append(ack(self.language))
            secondary = secondary_intent_line(decision, self.language)
            if secondary:
                parts.append(secondary)
            parts.append(followup_question(self.language))
            return self._apply_tone(" ".join(parts), decision)

        self.stage = ConversationStage.ENDED
        parts = []
        secondary = secondary_intent_line(decision, self.language)
        if secondary:
            parts.append(secondary)
        parts.append(closing_line_for_intent(decision, self.language))
        return self._apply_tone(" ".join(parts), decision)

    def _handle_promise_date(self, utterance: str) -> str:
        decision2 = self._classifier(ClassificationStage.PROMISE_DATE, self.consumer, utterance, self.transcript)
        notes = decision2.notes
        if decision2.promise_to_pay_date is None and notes:
            notes = f"Customer's own words on payment timing: {notes}"
        merged = self.decision.model_copy(
            update={
                "promise_to_pay_date": decision2.promise_to_pay_date,
                "notes": notes or self.decision.notes,
                "customer_angry": decision2.customer_angry,
            }
        )
        self.decision = merged
        self.stage = ConversationStage.ENDED
        return self._apply_tone(closing_line_for_intent(merged, self.language), merged)

    def _handle_followup(self, utterance: str) -> str:
        """The single response to whatever follow-up question was asked in
        _handle_main_response (e.g. "do you have the receipt?", "would you
        like this reviewed?"). Kept intentionally simple -- acknowledge and
        close -- rather than branching further per answer, matching the
        spec's own "If YES: thank you... [close]" pattern for every
        category."""
        decision = self._classifier(ClassificationStage.FOLLOWUP, self.consumer, utterance, self.transcript)
        if decision.do_not_call:
            self.stage = ConversationStage.ENDED
            return dnc_ack_line(self.language)
        self.stage = ConversationStage.ENDED
        return self._apply_tone(closing_line(self.language), decision)
