"""Pydantic schemas: enums, sheet-row contract, and the AI conversation
engine's output contract.

Anything derived from an LLM call passes through `LLMTurnOutput` /
`CallDecision` here before it is allowed to touch the database or the
sheet (spec Sec.39: "Never trust raw LLM output directly for database
updates.").
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Call job / attempt state machine (spec Sec.16)
# ---------------------------------------------------------------------------
class CallState(StrEnum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    DIALING = "DIALING"
    RINGING = "RINGING"
    CONNECTED = "CONNECTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    NO_ANSWER = "NO_ANSWER"
    BUSY = "BUSY"
    FAILED = "FAILED"
    CALLBACK_REQUESTED = "CALLBACK_REQUESTED"
    HUMAN_FOLLOWUP = "HUMAN_FOLLOWUP"
    DO_NOT_CALL = "DO_NOT_CALL"
    SKIPPED = "SKIPPED"


# States that make an attempt eligible for a retry (spec Sec.18).
RETRYABLE_STATES = {CallState.NO_ANSWER, CallState.BUSY, CallState.FAILED}

# Terminal states: a call_job in one of these is never re-queued.
TERMINAL_STATES = {
    CallState.COMPLETED,
    CallState.DO_NOT_CALL,
    CallState.SKIPPED,
}


# ---------------------------------------------------------------------------
# Customer response classification (spec Sec.25)
# ---------------------------------------------------------------------------
class CustomerIntent(StrEnum):
    ALREADY_PAID = "ALREADY_PAID"
    WILL_PAY_TODAY = "WILL_PAY_TODAY"
    WILL_PAY_TOMORROW = "WILL_PAY_TOMORROW"
    WILL_PAY_THIS_WEEK = "WILL_PAY_THIS_WEEK"
    PROMISE_TO_PAY = "PROMISE_TO_PAY"
    INSTALLMENT_REQUEST = "INSTALLMENT_REQUEST"
    NEEDS_MORE_TIME = "NEEDS_MORE_TIME"
    CALL_BACK = "CALL_BACK"
    DISPUTE = "DISPUTE"
    WRONG_NUMBER = "WRONG_NUMBER"
    WRONG_PERSON = "WRONG_PERSON"
    NOT_INTERESTED = "NOT_INTERESTED"
    DO_NOT_CALL = "DO_NOT_CALL"
    HUMAN_ASSISTANCE = "HUMAN_ASSISTANCE"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    NO_ANSWER = "NO_ANSWER"
    BUSY = "BUSY"
    VOICEMAIL = "VOICEMAIL"
    OTHER = "OTHER"


class AlreadyPaidStatus(StrEnum):
    NO = "NO"
    YES = "YES"
    CUSTOMER_CLAIMS_PAID = "CUSTOMER_CLAIMS_PAID"


class SupportedLanguage(StrEnum):
    URDU = "ur"
    ENGLISH = "en"


# ---------------------------------------------------------------------------
# Google Sheet row contract — this matches GSM Brothers' actual live sheet
# (spec Sec.5's column list was a hypothetical starting point; this project
# was retargeted to the real headers below once real data was available).
#
# Notable differences from the original hypothetical schema:
#   - There is no dedicated "Already Paid" / "Do Not Call" / "Human Follow-up"
#     column. Per an explicit decision, these are derived from the text in
#     "Call Status" / "Call out come" (see `_derive_already_paid` /
#     `_derive_do_not_call` / `_derive_human_followup` below) rather than
#     requiring new sheet columns. The local DoNotCall DB registry
#     (app/models.py) remains the authoritative compliance backstop
#     regardless of how this heuristic reads any given row.
#   - "DUES" is the single figure read aloud to consumers as the outstanding
#     balance (not "Total Due Billing" or "Recovery Amount", which are
#     stored but never spoken).
#   - CD, MRU, DUE BCM, LPD, LPA, IBC are opaque K-Electric reference codes:
#     stored/passed through untouched, no call-script or eligibility logic
#     is built on their meaning.
# ---------------------------------------------------------------------------
CONSUMER_NO_COLUMN = "Consumer No."

REQUIRED_SHEET_COLUMNS = [
    "Consumer No.",
    "Name",
    "Consumer Phone Number",
    "DUES",
    "Call Status",
    "Call Date",
]

ALL_SHEET_COLUMNS = [
    "SNo",
    "Contract",
    "Contract Account",
    "Consumer No.",
    "Meter No.",
    "CD",
    "MRU",
    "Name",
    "Address",
    "Rate Tariff",
    "DUE BCM",
    "Total Due Units",
    "Total Due Billing",
    "LPD",
    "LPA",
    "DUES",
    "Scheme eligibility",
    "Consumer Phone Number",
    "IBC",
    "Recovery Amount",
    "Remarks",
    "Call Date",
    "Call Time",
    "Call Atempt",
    "Call Status",
    "Call out come",
    "Transcript",
    "Recording URL",
    "Promise to Pay",
    "Date",
    "Agent Notes",
]


def _yes(value: str | None) -> bool:
    return (value or "").strip().upper() in {"YES", "Y", "TRUE", "1"}


def _num(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = str(value).replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _date(value: str | None) -> dt.date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    return None


_PAID_WORDS = ("PAID", "ALREADY PAID", "ALREADY_PAID")
_UNPAID_NEGATIONS = ("UNPAID", "NOT PAID", "NOT_PAID")
_DNC_WORDS = ("DO NOT CALL", "DO_NOT_CALL", "DNC")
_HUMAN_FOLLOWUP_OUTCOMES = ("DISPUTE", "INSTALLMENT_REQUEST", "HUMAN_ASSISTANCE", "ALREADY_PAID")


def _derive_already_paid(call_status: str | None, call_outcome: str | None) -> AlreadyPaidStatus:
    """No dedicated 'Already Paid' column exists in the real sheet — this is
    read from Call Status / Call out come text instead (see module docstring)."""
    outcome = (call_outcome or "").strip().upper()
    if outcome == "ALREADY_PAID":
        return AlreadyPaidStatus.CUSTOMER_CLAIMS_PAID
    status = (call_status or "").strip().upper()
    if any(neg in status for neg in _UNPAID_NEGATIONS):
        return AlreadyPaidStatus.NO
    if any(word in status for word in _PAID_WORDS):
        return AlreadyPaidStatus.CUSTOMER_CLAIMS_PAID
    return AlreadyPaidStatus.NO


def _derive_do_not_call(call_status: str | None, call_outcome: str | None) -> bool:
    """No dedicated 'Do Not Call' column — derived from Call Status / Call out
    come text. The local DoNotCall DB registry is the authoritative backstop
    regardless of this heuristic (see app/queue_manager.py)."""
    outcome = (call_outcome or "").strip().upper()
    if outcome == "DO_NOT_CALL":
        return True
    status = (call_status or "").strip().upper()
    return any(word in status for word in _DNC_WORDS)


def _derive_human_followup(call_outcome: str | None) -> bool:
    return (call_outcome or "").strip().upper() in _HUMAN_FOLLOWUP_OUTCOMES


class ConsumerRecord(BaseModel):
    """A validated, typed view of one Google Sheet row, built by header name."""

    consumer_no: str
    consumer_name: str | None = None
    mobile_number: str | None = None
    address: str | None = None

    # Reference/identifier fields, pass-through only — never used in call-script
    # or eligibility logic.
    sno: str | None = None
    contract: str | None = None
    contract_account: str | None = None
    meter_no: str | None = None
    cd: str | None = None
    mru: str | None = None
    due_bcm: str | None = None
    lpd: str | None = None
    lpa: str | None = None
    ibc: str | None = None

    outstanding_amount: float | None = None  # sourced from "DUES" — the figure spoken to consumers
    total_due_units: float | None = None
    total_due_billing: float | None = None
    recovery_amount: float | None = None

    due_date: dt.date | None = None  # no source column in the real sheet; stays None
    tariff: str | None = None
    installment_eligible: bool = False
    installment_details: str | None = None
    scheme_available: bool = False
    scheme_description: str | None = None
    status: str | None = None
    already_paid: AlreadyPaidStatus = AlreadyPaidStatus.NO
    promise_to_pay_flag: str | None = None
    promise_to_pay_date: dt.date | None = None
    remarks: str | None = None
    call_attempt: int = 0
    call_status: str | None = None
    call_outcome: str | None = None
    call_duration: int | None = None
    transcript: str | None = None
    recording_url: str | None = None
    last_call_date: dt.date | None = None
    last_call_time: str | None = None
    agent_notes: str | None = None
    human_followup: bool = False
    do_not_call: bool = False

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "ConsumerRecord":
        """Build from a {header_name: cell_value} dict (header lookup, not position)."""
        call_status = (row.get("Call Status") or "").strip() or None
        call_outcome = (row.get("Call out come") or "").strip() or None
        scheme_eligibility_raw = (row.get("Scheme eligibility") or "").strip() or None

        return cls(
            consumer_no=(row.get("Consumer No.") or "").strip(),
            consumer_name=(row.get("Name") or "").strip() or None,
            mobile_number=(row.get("Consumer Phone Number") or "").strip() or None,
            address=(row.get("Address") or "").strip() or None,
            sno=(row.get("SNo") or "").strip() or None,
            contract=(row.get("Contract") or "").strip() or None,
            contract_account=(row.get("Contract Account") or "").strip() or None,
            meter_no=(row.get("Meter No.") or "").strip() or None,
            cd=(row.get("CD") or "").strip() or None,
            mru=(row.get("MRU") or "").strip() or None,
            due_bcm=(row.get("DUE BCM") or "").strip() or None,
            lpd=(row.get("LPD") or "").strip() or None,
            lpa=(row.get("LPA") or "").strip() or None,
            ibc=(row.get("IBC") or "").strip() or None,
            outstanding_amount=_num(row.get("DUES")),
            total_due_units=_num(row.get("Total Due Units")),
            total_due_billing=_num(row.get("Total Due Billing")),
            recovery_amount=_num(row.get("Recovery Amount")),
            due_date=_date(row.get("Due Date")),
            tariff=(row.get("Rate Tariff") or "").strip() or None,
            installment_eligible=bool(
                scheme_eligibility_raw
                and scheme_eligibility_raw.strip().upper() not in ("NO", "NOT ELIGIBLE", "N", "0", "FALSE", "INELIGIBLE", "NONE")
            ),
            installment_details=None,
            scheme_available=bool(scheme_eligibility_raw),
            scheme_description=scheme_eligibility_raw,
            status=(row.get("Status") or "").strip() or None,
            already_paid=_derive_already_paid(call_status, call_outcome),
            promise_to_pay_flag=(row.get("Promise to Pay") or "").strip() or None,
            promise_to_pay_date=_date(row.get("Date")),
            remarks=(row.get("Remarks") or "").strip() or None,
            call_attempt=int(_num(row.get("Call Atempt")) or 0),
            call_status=call_status,
            call_outcome=call_outcome,
            call_duration=None,
            transcript=(row.get("Transcript") or "").strip() or None,
            recording_url=(row.get("Recording URL") or "").strip() or None,
            last_call_date=_date(row.get("Call Date")),
            last_call_time=(row.get("Call Time") or "").strip() or None,
            agent_notes=(row.get("Agent Notes") or "").strip() or None,
            human_followup=_derive_human_followup(call_outcome),
            do_not_call=_derive_do_not_call(call_status, call_outcome),
        )


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------
class TranscriptTurn(BaseModel):
    speaker: str  # "Agent" | "Customer"
    timestamp: dt.datetime
    message: str


# ---------------------------------------------------------------------------
# AI conversation engine output contract (spec Sec.39)
# ---------------------------------------------------------------------------
class CallDecision(BaseModel):
    """Structured, validated output of one conversation turn or a call's final outcome.

    The LLM is only ever asked to fill fields like `intent`/`detected_language`
    from the customer's transcribed speech — amounts/dates/scheme text are
    never sourced from the LLM, only echoed back from what the deterministic
    script already read from the ConsumerRecord.
    """

    intent: CustomerIntent
    detected_language: SupportedLanguage | None = None
    promise_to_pay_date: dt.date | None = None
    human_followup: bool = False
    do_not_call: bool = False
    verification_passed: bool | None = None
    next_action: str = "CONTINUE"  # "CONTINUE" | "END_CALL" | "TRANSFER_HUMAN"
    notes: str | None = None

    @field_validator("promise_to_pay_date")
    @classmethod
    def _no_past_dates_fabricated(cls, v: dt.date | None) -> dt.date | None:
        # Sanity guard only — the actual "don't manufacture a date" rule is
        # enforced upstream by only setting this field from an explicit
        # customer-provided date string, never letting the LLM invent one
        # when the customer gave a vague answer (see conversation_engine.py).
        return v


class ReportRow(BaseModel):
    date: dt.date
    total_consumers: int
    eligible_consumers: int
    calls_attempted: int
    calls_answered: int
    calls_completed: int
    no_answer: int
    busy: int
    wrong_number: int
    already_paid: int
    promise_to_pay: int
    installment_requests: int
    call_back: int
    disputes: int
    human_followup: int
    do_not_call: int
    failed_calls: int
    total_outstanding_contacted: float
    total_promise_to_pay_amount: float
    average_call_duration_seconds: float


class SchedulerStatus(StrEnum):
    WAITING = "WAITING"
    CALLING = "CALLING"
    BREAK = "BREAK"
    DAY_COMPLETED = "DAY_COMPLETED"
    WAITING_FOR_NEXT_DAY = "WAITING_FOR_NEXT_DAY"


class CampaignStatus(StrEnum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
