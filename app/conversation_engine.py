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
import re
from enum import StrEnum

from app.config import ConfigurationError, get_settings
from app.schemas import CallDecision, ConsumerRecord, CustomerIntent, SupportedLanguage, TranscriptTurn
from app.utils import normalize_pakistani_mobile

logger = logging.getLogger("calls")

_PHONE_CANDIDATE_RE = re.compile(r"\d[\d\s\-]{8,14}\d")


def _extract_phone_number(text: str) -> str | None:
    """Deterministic (non-LLM) Pakistani mobile number extraction -- scans
    for digit-like substrings of plausible phone-number length and validates
    each with the same normalizer already used for sheet/DB numbers, rather
    than trusting an LLM to transcribe digits reliably (spec 2026-08-09:
    ALTERNATE_OWNER_CONTACT / PAYMENT_CONTACT_NUMBER capture)."""
    for candidate in _PHONE_CANDIDATE_RE.findall(text):
        normalized = normalize_pakistani_mobile(candidate)
        if normalized:
            return normalized
    return None


_AFFIRM_PHRASES = ("yes", "yeah", "yep", "sure", "ji han", "ji haan", "haan", "bilkul", "sahi hai", "theek hai", "correct")
_DENY_PHRASES = ("no ", "no,", "no.", "nahi", "nahin", "galat hai", "wrong", "not correct", "sahi nahi")


def _is_affirmative(text: str) -> bool | None:
    """Lightweight bilingual yes/no heuristic for pure procedural
    confirmation turns (address confirmation, installment interest) -- these
    are meta-conversation yes/no questions the general-purpose intent
    classifier isn't well-suited to represent, so a direct phrase check
    (matching the pattern keyword_fallback_classifier already uses for
    identity-verification yes/no) is more reliable than routing through
    CustomerIntent alone. Returns None (ambiguous) rather than guessing when
    neither list matches."""
    t = f" {text.lower()} "
    if any(p in t for p in _DENY_PHRASES):
        return False
    if any(p in t for p in _AFFIRM_PHRASES):
        return True
    return None


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


def address_confirmation_question_line(consumer: ConsumerRecord, language: SupportedLanguage) -> str:
    """Spec 2026-08-09 Address Rule: on a wrong-address claim, never
    disconnect -- first read back the plot/house/address on record and ask
    the customer to confirm it, since a hasty or mis-transcribed "not my
    address" shouldn't immediately escalate to asking for someone else's
    contact details."""
    address = consumer.address
    if language == SupportedLanguage.URDU:
        if address:
            return f"Hamare record ke mutabiq is account ka pata/plot ye hai: {address}. Kya ye aap ka mojooda ghar ya plot hai?"
        return "Kya aap barah-e-karam is account ka registered plot ya ghar number confirm kar sakte hain?"
    if address:
        return f"According to our record, this account's registered address/plot is: {address}. Is this your current house or plot?"
    return "Could you please confirm the plot or house number registered against this account?"


def alternate_contact_request_line(language: SupportedLanguage) -> str:
    """Exact wording specified by the user (2026-08-09) for when the customer
    still denies the address after confirmation -- asks for the actual
    owner/current occupant's contact instead of disconnecting or making any
    threat/legal claim."""
    if language == SupportedLanguage.URDU:
        return (
            "جی، ہمارے ریکارڈ کے مطابق اس موجودہ پلاٹ/گھر نمبر پر KE کے واجبات موجود ہیں۔ ہم ان واجبات کی ادائیگی کے "
            "لیے متعلقہ مالک یا موجودہ صارف سے رابطہ کرنا چاہتے ہیں۔ اگر آپ کے پاس اصل مالک یا موجودہ صارف کا رابطہ "
            "نمبر ہے تو براہِ کرم فراہم کر دیں۔"
        )
    return (
        "I understand. According to our record, K-Electric dues exist against this current plot/house number. "
        "We would like to contact the relevant owner or current occupant to settle these dues. If you have a "
        "contact number for the actual owner or current occupant, please share it with us."
    )


def alternate_contact_saved_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return "Shukriya, hum ne ye number note kar liya hai. Hamara numainda un se raabta karega."
    return "Thank you, we've noted that number. Our representative will reach out to them."


def alternate_contact_not_provided_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return "Theek hai, koi baat nahin. Hum apne mojooda record ke mutabiq is maamle ko age follow-up ke liye bhej rahe hain."
    return "That's alright. We'll proceed with this matter for follow-up based on our existing record."


def installment_offer_question_line(language: SupportedLanguage) -> str:
    """Exact wording specified by the user (2026-08-09) -- spoken ONLY after
    the customer says they cannot pay the full amount; never mentioned
    proactively (spec 2026-08-09 Dues & Installment Logic)."""
    if language == SupportedLanguage.URDU:
        return (
            "جی، میں آپ کی بات سمجھ رہا ہوں۔ ہمارے پاس scheme/installment کا option بھی موجود ہے۔ آپ اس کے بارے میں "
            "کیا خیال رکھتے ہیں؟"
        )
    return (
        "I understand your situation. We also have a scheme/installment option available. What are your thoughts "
        "on that?"
    )


def installment_declined_closing_line(language: SupportedLanguage) -> str:
    if language == SupportedLanguage.URDU:
        return "Theek hai, samajh gayi. Waqt dene ka shukriya. Allah Hafiz."
    return "Understood. Thank you for your time. Goodbye."


def payment_contact_confirm_question_line(consumer: ConsumerRecord, language: SupportedLanguage) -> str:
    number = consumer.mobile_number
    if language == SupportedLanguage.URDU:
        if number:
            return f"Theek hai. Kya hum installment ki tafseel is number par bhej dein: {number}? Ya koi aur number dena chahenge?"
        return "Theek hai. Barah-e-karam wo number bata dein jis par hum installment ki tafseel bhej sakein."
    if number:
        return f"Alright. Shall we send the installment details to this number: {number}? Or would you like to provide a different number?"
    return "Alright. Please share the number where we can send the installment details."


def payment_contact_saved_line(number: str | None, language: SupportedLanguage) -> str:
    """Never invents installment amounts/terms -- only confirms where the
    (separately, already-approved) details will be sent, with the nearest-KE
    -Customer-Service-Centre fallback per spec 2026-08-09 when PDF delivery
    isn't available."""
    if language == SupportedLanguage.URDU:
        if number:
            return (
                f"Shukriya. Hum installment/bill ki PDF {number} par bhejne ki koshish karein ge. Agar PDF na milay "
                "to barah-e-karam apne qareeb tareen KE Customer Service Centre se installment ki tafseelat hasil "
                "kar ke payment kar dein."
            )
        return (
            "Theek hai. Barah-e-karam apne qareeb tareen KE Customer Service Centre se installment ki tafseelat "
            "hasil kar ke payment kar dein."
        )
    if number:
        return (
            f"Thank you. We'll try to send the installment/bill PDF to {number}. If it doesn't arrive, please "
            "obtain the installment details from your nearest KE Customer Service Centre and make the payment there."
        )
    return (
        "Alright. Please obtain the installment details from your nearest KE Customer Service Centre and make the "
        "payment there."
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
    CustomerIntent.COMPLAINT_NOT_ADDRESSED: complaint_not_addressed_line,
    # NOT_MY_ADDRESS deliberately absent: it gets its own dedicated
    # confirm-then-escalate flow (_handle_address_confirmation /
    # _handle_alternate_contact), not a single ack+question+close.
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
    ADDRESS_CONFIRMATION = "ADDRESS_CONFIRMATION"
    INSTALLMENT_INTEREST = "INSTALLMENT_INTEREST"


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
- Never set alternate_owner_contact or payment_contact_number yourself -- leave both null. They're filled in by \
the system elsewhere from the transcript directly, not by you.
- If the utterance raises a second, genuinely distinct concern alongside the primary one (e.g. "I already paid \
AND my complaint hasn't been resolved"), set secondary_intent to that second concern's category. Only use this \
for a real second concern, not a restatement of the same one.
- Set customer_angry=true if the tone is hostile, frustrated, or raised -- independent of which category the \
content falls into.
- verification_passed only matters at the VERIFY_IDENTITY stage, right after the agent asked whether it's \
speaking with the named consumer -- setting it to false immediately ends the call (verification-failed line, no \
further conversation), so it is a high-cost, one-way decision. Set verification_passed=false ONLY if the customer \
explicitly said this is the wrong person / wrong number / not them. For anything else at that stage -- "yes", a \
garbled or partial transcript, filler words, an unrelated or unclear utterance, or literally any response that \
isn't an explicit wrong-person/wrong-number denial -- set verification_passed=true and let the call continue; a \
transcription error should never be treated as proof the wrong person answered.
- At the ADDRESS_CONFIRMATION stage (the agent just read back the address/plot on record and asked the customer \
to confirm it, after they first claimed it wasn't their address): set intent=NOT_MY_ADDRESS ONLY if the customer \
still explicitly denies it's their address/plot. Anything else -- confirmation, an unclear or garbled response, \
silence-filler words -- set intent=OTHER, since a mis-transcription should never be treated as a second denial.
- At the INSTALLMENT_INTEREST stage (the agent just asked whether the customer wants the installment/scheme \
option, after the customer said they can't pay the full amount): set intent=INSTALLMENT_REQUEST if they want it; \
REFUSES_TO_PAY or NOT_INTERESTED if they explicitly decline; OTHER if unclear.
- Current conversation stage: {stage}
"""


def _build_anthropic_client(settings):
    settings.require_ai()
    from anthropic import Anthropic

    return Anthropic(api_key=settings.ai_api_key)


def _resolve_refs(node, defs: dict):
    """Inline pydantic's `$ref: "#/$defs/X"` schema references. DeepSeek's
    OpenAI-compatible function calling tolerates raw pydantic JSON Schema
    ($defs/$ref/anyOf) the same way Anthropic does, but Gemini's function
    schema (an OpenAPI-3.0 subset) doesn't resolve $ref at all -- it has to
    be inlined before `_to_gemini_schema` can touch it."""
    if isinstance(node, dict):
        if "$ref" in node:
            ref_name = node["$ref"].rsplit("/", 1)[-1]
            resolved = _resolve_refs(defs[ref_name], defs)
            overrides = {k: v for k, v in node.items() if k != "$ref"}
            return {**resolved, **overrides}
        return {k: _resolve_refs(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_resolve_refs(v, defs) for v in node]
    return node


def _to_gemini_schema(schema: dict) -> dict:
    """Best-effort translation of CallDecision's pydantic JSON Schema into
    Gemini's function-parameter schema: inline $defs/$ref (Gemini can't
    resolve them), collapse pydantic's `anyOf: [T, {type: null}]` "optional"
    pattern into `nullable: true`, and uppercase JSON Schema type names to
    Gemini's OpenAPI Type enum (STRING/OBJECT/...). Not a general-purpose
    JSON Schema -> OpenAPI converter -- only handles the shapes pydantic
    actually emits for this one model."""

    def convert(node):
        if not isinstance(node, dict):
            return node
        node = dict(node)
        node.pop("title", None)
        node.pop("format", None)
        any_of = node.pop("anyOf", None)
        if any_of is not None:
            non_null = [o for o in any_of if o.get("type") != "null"]
            nullable = len(non_null) != len(any_of)
            if len(non_null) == 1:
                converted = convert(non_null[0])
                if nullable:
                    converted["nullable"] = True
                return converted
            node["oneOf"] = [convert(o) for o in non_null]
        if "type" in node:
            node["type"] = node["type"].upper()
        elif "enum" in node:
            node["type"] = "STRING"
        if node.get("type") == "OBJECT" and "properties" in node:
            node["properties"] = {k: convert(v) for k, v in node["properties"].items()}
        if node.get("type") == "ARRAY" and "items" in node:
            node["items"] = convert(node["items"])
        return node

    defs = schema.get("$defs", {})
    return convert(_resolve_refs(schema, defs))


def _sanitize_decision(decision: CallDecision, utterance: str) -> CallDecision:
    """Defensive guard against a fabricated promise-to-pay date, and against
    the LLM ever populating alternate_owner_contact/payment_contact_number
    itself -- those two fields exist on CallDecision (and therefore in every
    provider's tool schema) only so the engine can carry a value it already
    extracted deterministically via _extract_phone_number(); nulling them
    here on every classifier response, regardless of stage, means a model
    that decides to "helpfully" fill one in on an unrelated turn can never
    leak a hallucinated number into a saved call record."""
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
    if decision.alternate_owner_contact is not None or decision.payment_contact_number is not None:
        decision = decision.model_copy(update={"alternate_owner_contact": None, "payment_contact_number": None})
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


def _no_tool_call_decision(provider: str, stage: ClassificationStage) -> CallDecision:
    logger.warning("%s returned no tool/function call for stage=%s", provider, stage.value)
    return CallDecision(
        intent=CustomerIntent.OTHER,
        human_followup=True,
        notes="LLM returned no classification; routed to human review.",
    )


def _invalid_payload_decision(provider: str, payload) -> CallDecision:
    logger.exception("%s returned an invalid CallDecision payload: %r", provider, payload)
    return CallDecision(
        intent=CustomerIntent.OTHER,
        human_followup=True,
        notes="LLM output failed schema validation; routed to human review.",
    )


def _classify_with_openai_compatible(
    provider_name: str,
    base_url: str,
    api_key: str,
    model: str,
    stage: ClassificationStage,
    utterance: str,
    history: list[TranscriptTurn],
    extra_headers: dict | None = None,
) -> CallDecision:
    """Shared body for any provider that speaks the OpenAI chat/completions +
    tool-calling wire format -- DeepSeek and OpenRouter both do, and it
    accepts the same raw pydantic JSON Schema Anthropic does (no $ref
    inlining needed, unlike Gemini)."""
    import httpx

    schema = CallDecision.model_json_schema()
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(stage=stage.value)
    transcript_text = "\n".join(f"{t.speaker}: {t.message}" for t in history)

    response = httpx.post(
        base_url,
        headers={"Authorization": f"Bearer {api_key}", **(extra_headers or {})},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": f"Conversation so far:\n{transcript_text}\n\nLatest customer utterance: {utterance!r}",
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "classify_customer_response",
                        "description": "Structured classification of the customer's utterance.",
                        "parameters": schema,
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "classify_customer_response"}},
        },
        timeout=30.0,
    )
    response.raise_for_status()
    tool_calls = response.json()["choices"][0]["message"].get("tool_calls") or []
    if not tool_calls:
        return _no_tool_call_decision(provider_name, stage)

    import json as _json

    raw_arguments = tool_calls[0]["function"]["arguments"]
    try:
        decision = CallDecision.model_validate(_json.loads(raw_arguments))
    except Exception:
        return _invalid_payload_decision(provider_name, raw_arguments)
    return _sanitize_decision(decision, utterance)


def _classify_with_deepseek(
    stage: ClassificationStage,
    consumer: ConsumerRecord,
    utterance: str,
    history: list[TranscriptTurn] | None = None,
    settings=None,
) -> CallDecision:
    settings = settings or get_settings()
    settings.require_deepseek()
    return _classify_with_openai_compatible(
        "DeepSeek", "https://api.deepseek.com/chat/completions", settings.deepseek_api_key,
        settings.deepseek_model, stage, utterance, history or [],
    )


def _classify_with_openrouter(
    stage: ClassificationStage,
    consumer: ConsumerRecord,
    utterance: str,
    history: list[TranscriptTurn] | None = None,
    settings=None,
) -> CallDecision:
    settings = settings or get_settings()
    settings.require_openrouter()
    return _classify_with_openai_compatible(
        "OpenRouter", "https://openrouter.ai/api/v1/chat/completions", settings.openrouter_api_key,
        settings.openrouter_model, stage, utterance, history or [],
        extra_headers={"HTTP-Referer": "https://gsmbrothers.local", "X-Title": "GSM Brothers AI Recovery Agent"},
    )


def _classify_with_gemini(
    stage: ClassificationStage,
    consumer: ConsumerRecord,
    utterance: str,
    history: list[TranscriptTurn] | None = None,
    settings=None,
) -> CallDecision:
    settings = settings or get_settings()
    settings.require_gemini()
    import httpx

    history = history or []
    schema = _to_gemini_schema(CallDecision.model_json_schema())
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(stage=stage.value)
    transcript_text = "\n".join(f"{t.speaker}: {t.message}" for t in history)
    user_text = f"Conversation so far:\n{transcript_text}\n\nLatest customer utterance: {utterance!r}"

    response = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent",
        params={"key": settings.gemini_api_key},
        json={
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "tools": [
                {
                    "function_declarations": [
                        {
                            "name": "classify_customer_response",
                            "description": "Structured classification of the customer's utterance.",
                            "parameters": schema,
                        }
                    ]
                }
            ],
            "tool_config": {
                "function_calling_config": {"mode": "ANY", "allowed_function_names": ["classify_customer_response"]}
            },
        },
        timeout=30.0,
    )
    response.raise_for_status()
    candidates = response.json().get("candidates") or []
    parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
    for part in parts:
        if "functionCall" in part:
            args = part["functionCall"].get("args", {})
            try:
                decision = CallDecision.model_validate(args)
            except Exception:
                return _invalid_payload_decision("Gemini", args)
            return _sanitize_decision(decision, utterance)
    return _no_tool_call_decision("Gemini", stage)


_PROVIDER_KEY_ATTR = {
    "anthropic": "ai_api_key",
    "deepseek": "deepseek_api_key",
    "gemini": "gemini_api_key",
    "openrouter": "openrouter_api_key",
}


def _classify_with_provider(
    provider: str,
    stage: ClassificationStage,
    consumer: ConsumerRecord,
    utterance: str,
    history: list[TranscriptTurn] | None,
    settings,
) -> CallDecision:
    # Dispatches by looking up the module-global name at call time (rather
    # than a dict built from these functions' identities at import time) so
    # that `mocker.patch("app.conversation_engine.classify_with_llm", ...)`
    # style test patches actually take effect here, same as everywhere else
    # in this module.
    if provider == "anthropic":
        return classify_with_llm(stage, consumer, utterance, history, settings=settings)
    if provider == "deepseek":
        return _classify_with_deepseek(stage, consumer, utterance, history, settings=settings)
    if provider == "gemini":
        return _classify_with_gemini(stage, consumer, utterance, history, settings=settings)
    if provider == "openrouter":
        return _classify_with_openrouter(stage, consumer, utterance, history, settings=settings)
    raise ConfigurationError(f"unknown LLM provider {provider!r} in LLM_FALLBACK_ORDER")


def _llm_classifier_with_fallback(
    stage: ClassificationStage,
    consumer: ConsumerRecord,
    utterance: str,
    history: list[TranscriptTurn] | None = None,
    settings=None,
) -> CallDecision:
    """Tries each configured provider in settings.LLM_FALLBACK_ORDER before
    giving up and degrading to the offline keyword classifier for this turn.

    Originally this only wrapped classify_with_llm (Anthropic) -- added
    2026-08-08 after a live call hit an Anthropic account with a zero credit
    balance mid-conversation, so a network/API failure never propagates up
    and ends the call. Extended the same day to also try DeepSeek and
    Gemini as configured backups before falling all the way back to
    keywords, rather than degrading straight to keyword matching the moment
    Anthropic alone has a bad day."""
    settings = settings or get_settings()
    order = [p.strip().lower() for p in settings.llm_fallback_order.split(",") if p.strip()]

    for provider in order:
        key_attr = _PROVIDER_KEY_ATTR.get(provider)
        if key_attr is None:
            logger.warning("unknown provider %r in LLM_FALLBACK_ORDER; skipping", provider)
            continue
        if not getattr(settings, key_attr):
            continue
        try:
            return _classify_with_provider(provider, stage, consumer, utterance, history, settings)
        except Exception:
            logger.exception("%s classifier failed for stage=%s; trying next provider", provider, stage.value)

    logger.error("no configured LLM provider succeeded; falling back to keyword_fallback_classifier for this turn")
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
    AWAITING_ADDRESS_CONFIRMATION = "AWAITING_ADDRESS_CONFIRMATION"
    AWAITING_ALTERNATE_CONTACT = "AWAITING_ALTERNATE_CONTACT"
    AWAITING_INSTALLMENT_INTEREST = "AWAITING_INSTALLMENT_INTEREST"
    AWAITING_PAYMENT_CONTACT = "AWAITING_PAYMENT_CONTACT"
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
        if any(getattr(settings, attr) for attr in _PROVIDER_KEY_ATTR.values()):
            return _llm_classifier_with_fallback
        logger.warning(
            "No LLM provider configured (AI_API_KEY/DEEPSEEK_API_KEY/GEMINI_API_KEY/OPENROUTER_API_KEY); "
            "using offline keyword_fallback_classifier (not for production)"
        )
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
        elif self.stage == ConversationStage.AWAITING_ADDRESS_CONFIRMATION:
            line = self._handle_address_confirmation(utterance)
        elif self.stage == ConversationStage.AWAITING_ALTERNATE_CONTACT:
            line = self._handle_alternate_contact(utterance)
        elif self.stage == ConversationStage.AWAITING_INSTALLMENT_INTEREST:
            line = self._handle_installment_interest(utterance)
        elif self.stage == ConversationStage.AWAITING_PAYMENT_CONTACT:
            line = self._handle_payment_contact(utterance)
        else:
            line = ""

        logger.info(
            "INTENT_CLASSIFIED consumer_no=%s intent=%s secondary_intent=%s angry=%s",
            self.consumer.consumer_no, self.decision.intent.value,
            self.decision.secondary_intent.value if self.decision.secondary_intent else None,
            self.decision.customer_angry,
        )
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

        # NOT_MY_ADDRESS gets its own confirm-then-escalate flow (spec
        # 2026-08-09 Address Rule) rather than the generic ack+question+close
        # pattern -- a hasty or mis-transcribed claim shouldn't immediately
        # jump to asking for someone else's contact details.
        if decision.intent == CustomerIntent.NOT_MY_ADDRESS:
            self.stage = ConversationStage.AWAITING_ADDRESS_CONFIRMATION
            return self._apply_tone(address_confirmation_question_line(self.consumer, self.language), decision)

        # Installment is only ever offered after the customer says they
        # can't pay -- never proactively (spec 2026-08-09 Dues & Installment
        # Logic) -- so NEEDS_MORE_TIME gets its own offer-then-confirm flow
        # instead of the generic ack+question+close pattern.
        if decision.intent == CustomerIntent.NEEDS_MORE_TIME:
            self.stage = ConversationStage.AWAITING_INSTALLMENT_INTEREST
            return self._apply_tone(installment_offer_question_line(self.language), decision)

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

    def _handle_address_confirmation(self, utterance: str) -> str:
        """Response to "our record shows plot/address X, is that where you
        are?" (spec 2026-08-09 Address Rule). Only an explicit second denial
        escalates to asking for someone else's contact -- the classification
        prompt for ADDRESS_CONFIRMATION already treats confirmation, silence
        filler, or a garbled/unclear response as intent=OTHER, never a
        second NOT_MY_ADDRESS, so a transcription hiccup can't accidentally
        trigger the escalation."""
        decision = self._classifier(ClassificationStage.ADDRESS_CONFIRMATION, self.consumer, utterance, self.transcript)
        self.decision = decision

        if decision.do_not_call:
            self.stage = ConversationStage.ENDED
            return dnc_ack_line(self.language)

        if decision.intent == CustomerIntent.NOT_MY_ADDRESS:
            self.stage = ConversationStage.AWAITING_ALTERNATE_CONTACT
            return self._apply_tone(alternate_contact_request_line(self.language), decision)

        # Confirmed (or unclear) -- resume the normal conversation rather
        # than treating a mis-transcription as a second denial.
        self.stage = ConversationStage.AWAITING_MAIN_RESPONSE
        return self._apply_tone(main_question_line(self.language), decision)

    def _handle_alternate_contact(self, utterance: str) -> str:
        """Deterministic, no LLM call -- capturing a phone number correctly
        is a job for the same normalizer already used on sheet/DB numbers,
        not something to trust an LLM's transcription-of-digits to get
        right (see _extract_phone_number)."""
        phone = _extract_phone_number(utterance)
        self.stage = ConversationStage.ENDED
        if phone:
            self.decision = self.decision.model_copy(update={"alternate_owner_contact": phone})
            return self._apply_tone(alternate_contact_saved_line(self.language), self.decision)
        return self._apply_tone(alternate_contact_not_provided_line(self.language), self.decision)

    def _handle_installment_interest(self, utterance: str) -> str:
        """Response to "we also have an installment option, interested?" --
        only reached after the customer already said they can't pay (spec
        2026-08-09 Dues & Installment Logic: never offered proactively)."""
        decision = self._classifier(ClassificationStage.INSTALLMENT_INTEREST, self.consumer, utterance, self.transcript)
        self.decision = decision

        if decision.do_not_call:
            self.stage = ConversationStage.ENDED
            return dnc_ack_line(self.language)

        if decision.intent == CustomerIntent.INSTALLMENT_REQUEST:
            self.stage = ConversationStage.AWAITING_PAYMENT_CONTACT
            return self._apply_tone(payment_contact_confirm_question_line(self.consumer, self.language), decision)

        if decision.intent in (CustomerIntent.REFUSES_TO_PAY, CustomerIntent.NOT_INTERESTED):
            self.stage = ConversationStage.ENDED
            return self._apply_tone(installment_declined_closing_line(self.language), decision)

        # Unclear -- don't assume consent; close politely via the generic path.
        self.stage = ConversationStage.ENDED
        return self._apply_tone(closing_line_for_intent(decision, self.language), decision)

    def _handle_payment_contact(self, utterance: str) -> str:
        """Deterministic, no LLM call -- same reasoning as
        _handle_alternate_contact. An affirmative with no new number given
        means "yes, send it to the number you already called me on"."""
        phone = _extract_phone_number(utterance)
        if phone is None and _is_affirmative(utterance) is True:
            phone = self.consumer.mobile_number
        self.stage = ConversationStage.ENDED
        if phone:
            self.decision = self.decision.model_copy(update={"payment_contact_number": phone})
        return self._apply_tone(payment_contact_saved_line(phone, self.language), self.decision)
