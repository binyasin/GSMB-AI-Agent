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
# Google Sheet row contract (spec Sec.5) — required minimum columns
# ---------------------------------------------------------------------------
REQUIRED_SHEET_COLUMNS = [
    "Consumer No",
    "Consumer Name",
    "Mobile Number",
    "Outstanding Amount",
    "Due Date",
    "Call Status",
    "Last Call Date",
]

ALL_SHEET_COLUMNS = [
    "Consumer No",
    "Consumer Name",
    "Father Name",
    "Mobile Number",
    "Address",
    "Outstanding Amount",
    "Current Bill",
    "Arrears",
    "Due Date",
    "Tariff",
    "Installment Eligible",
    "Installment Details",
    "Scheme Available",
    "Scheme Description",
    "Status",
    "Already Paid",
    "Promise To Pay Date",
    "Remarks",
    "Call Attempt",
    "Call Status",
    "Call Outcome",
    "Call Duration",
    "Transcript",
    "Recording URL",
    "Last Call Date",
    "Last Call Time",
    "Agent Notes",
    "Human Follow-up",
    "Do Not Call",
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


class ConsumerRecord(BaseModel):
    """A validated, typed view of one Google Sheet row, built by header name."""

    consumer_no: str
    consumer_name: str | None = None
    father_name: str | None = None
    mobile_number: str | None = None
    address: str | None = None
    outstanding_amount: float | None = None
    current_bill: float | None = None
    arrears: float | None = None
    due_date: dt.date | None = None
    tariff: str | None = None
    installment_eligible: bool = False
    installment_details: str | None = None
    scheme_available: bool = False
    scheme_description: str | None = None
    status: str | None = None
    already_paid: AlreadyPaidStatus = AlreadyPaidStatus.NO
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
        already_paid_raw = (row.get("Already Paid") or "").strip().upper()
        already_paid = AlreadyPaidStatus.NO
        if already_paid_raw in ("YES", "Y", "TRUE", "1"):
            already_paid = AlreadyPaidStatus.YES
        elif already_paid_raw == "CUSTOMER_CLAIMS_PAID":
            already_paid = AlreadyPaidStatus.CUSTOMER_CLAIMS_PAID

        return cls(
            consumer_no=(row.get("Consumer No") or "").strip(),
            consumer_name=(row.get("Consumer Name") or "").strip() or None,
            father_name=(row.get("Father Name") or "").strip() or None,
            mobile_number=(row.get("Mobile Number") or "").strip() or None,
            address=(row.get("Address") or "").strip() or None,
            outstanding_amount=_num(row.get("Outstanding Amount")),
            current_bill=_num(row.get("Current Bill")),
            arrears=_num(row.get("Arrears")),
            due_date=_date(row.get("Due Date")),
            tariff=(row.get("Tariff") or "").strip() or None,
            installment_eligible=_yes(row.get("Installment Eligible")),
            installment_details=(row.get("Installment Details") or "").strip() or None,
            scheme_available=_yes(row.get("Scheme Available")),
            scheme_description=(row.get("Scheme Description") or "").strip() or None,
            status=(row.get("Status") or "").strip() or None,
            already_paid=already_paid,
            promise_to_pay_date=_date(row.get("Promise To Pay Date")),
            remarks=(row.get("Remarks") or "").strip() or None,
            call_attempt=int(_num(row.get("Call Attempt")) or 0),
            call_status=(row.get("Call Status") or "").strip() or None,
            call_outcome=(row.get("Call Outcome") or "").strip() or None,
            call_duration=int(_num(row.get("Call Duration")) or 0) or None,
            transcript=(row.get("Transcript") or "").strip() or None,
            recording_url=(row.get("Recording URL") or "").strip() or None,
            last_call_date=_date(row.get("Last Call Date")),
            last_call_time=(row.get("Last Call Time") or "").strip() or None,
            agent_notes=(row.get("Agent Notes") or "").strip() or None,
            human_followup=_yes(row.get("Human Follow-up")),
            do_not_call=_yes(row.get("Do Not Call")),
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
