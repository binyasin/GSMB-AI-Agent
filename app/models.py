"""SQLAlchemy ORM models.

Tables (spec Sec.34): consumers, call_jobs, call_attempts, call_events,
daily_sessions, agent_settings, do_not_call, audit_logs.

State/outcome values are plain strings validated against the enums in
`schemas.py` at the application layer (not DB CHECK constraints), so the
schema stays portable between SQLite (dev) and PostgreSQL (prod) without
migrations for every new enum member.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Consumer(Base):
    """Local mirror of one Google Sheet row, keyed by Consumer No."""

    __tablename__ = "consumers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consumer_no: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    consumer_name: Mapped[str | None] = mapped_column(String(255))
    mobile_number: Mapped[str | None] = mapped_column(String(32))
    address: Mapped[str | None] = mapped_column(Text)

    # Opaque K-Electric reference/identifier fields — stored/passed through
    # only, no call-script or eligibility logic depends on their meaning.
    sno: Mapped[str | None] = mapped_column(String(32))
    contract: Mapped[str | None] = mapped_column(String(64))
    contract_account: Mapped[str | None] = mapped_column(String(64))
    meter_no: Mapped[str | None] = mapped_column(String(64))
    cd: Mapped[str | None] = mapped_column(String(64))
    mru: Mapped[str | None] = mapped_column(String(64))
    due_bcm: Mapped[str | None] = mapped_column(String(64))
    lpd: Mapped[str | None] = mapped_column(String(64))
    lpa: Mapped[str | None] = mapped_column(String(64))
    ibc: Mapped[str | None] = mapped_column(String(64))

    outstanding_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))  # from "DUES" — spoken to consumers
    total_due_units: Mapped[float | None] = mapped_column(Numeric(14, 2))
    total_due_billing: Mapped[float | None] = mapped_column(Numeric(14, 2))
    recovery_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    due_date: Mapped[dt.date | None] = mapped_column(Date)
    tariff: Mapped[str | None] = mapped_column(String(64))

    installment_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    installment_details: Mapped[str | None] = mapped_column(Text)
    scheme_available: Mapped[bool] = mapped_column(Boolean, default=False)
    scheme_description: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str | None] = mapped_column(String(64))
    already_paid: Mapped[str] = mapped_column(String(32), default="NO")
    promise_to_pay_flag: Mapped[str | None] = mapped_column(String(32))
    promise_to_pay_date: Mapped[dt.date | None] = mapped_column(Date)
    remarks: Mapped[str | None] = mapped_column(Text)

    call_attempt: Mapped[int] = mapped_column(Integer, default=0)
    call_status: Mapped[str] = mapped_column(String(32), default="PENDING")
    call_outcome: Mapped[str | None] = mapped_column(String(32))
    call_duration: Mapped[int | None] = mapped_column(Integer)
    transcript: Mapped[str | None] = mapped_column(Text)
    recording_url: Mapped[str | None] = mapped_column(Text)
    last_call_date: Mapped[dt.date | None] = mapped_column(Date)
    last_call_time: Mapped[dt.time | None] = mapped_column(Time)
    agent_notes: Mapped[str | None] = mapped_column(Text)

    human_followup: Mapped[bool] = mapped_column(Boolean, default=False)
    do_not_call: Mapped[bool] = mapped_column(Boolean, default=False)

    # Captured deterministically from the transcript (never LLM-authored,
    # see conversation_engine._extract_phone_number) -- spec 2026-08-09
    # Address Rule / Dues & Installment Logic.
    alternate_owner_contact: Mapped[str | None] = mapped_column(String(32))
    payment_contact_number: Mapped[str | None] = mapped_column(String(32))

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    call_jobs: Mapped[list["CallJob"]] = relationship(back_populates="consumer")


class CallJob(Base):
    """One row per (consumer, calling day): the unit of duplicate-call protection."""

    __tablename__ = "call_jobs"
    __table_args__ = (UniqueConstraint("consumer_no", "job_date", name="uq_call_job_consumer_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_uid: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    consumer_id: Mapped[int] = mapped_column(ForeignKey("consumers.id"), nullable=False)
    consumer_no: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    job_date: Mapped[dt.date] = mapped_column(Date, nullable=False)

    state: Mapped[str] = mapped_column(String(32), default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)

    locked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(128))

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    consumer: Mapped["Consumer"] = relationship(back_populates="call_jobs")
    attempts: Mapped[list["CallAttempt"]] = relationship(back_populates="call_job")


class CallAttempt(Base):
    """One row per actual dial attempt against a CallJob."""

    __tablename__ = "call_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_uid: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    call_job_id: Mapped[int] = mapped_column(ForeignKey("call_jobs.id"), nullable=False)
    consumer_no: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)

    provider_call_sid: Mapped[str | None] = mapped_column(String(128), index=True)
    attempt_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    attempt_time: Mapped[dt.time] = mapped_column(Time, nullable=False)

    result: Mapped[str] = mapped_column(String(32), default="DIALING")
    call_outcome: Mapped[str | None] = mapped_column(String(32))
    call_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    promise_to_pay_date: Mapped[dt.date | None] = mapped_column(Date)
    recording_url: Mapped[str | None] = mapped_column(Text)
    transcript_json: Mapped[str | None] = mapped_column(Text)
    agent_notes: Mapped[str | None] = mapped_column(Text)
    verification_passed: Mapped[bool | None] = mapped_column(Boolean)
    human_followup: Mapped[bool] = mapped_column(Boolean, default=False)
    do_not_call: Mapped[bool] = mapped_column(Boolean, default=False)
    alternate_owner_contact: Mapped[str | None] = mapped_column(String(32))
    payment_contact_number: Mapped[str | None] = mapped_column(String(32))

    sheet_synced: Mapped[bool] = mapped_column(Boolean, default=False)
    sheet_sync_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    call_job: Mapped["CallJob"] = relationship(back_populates="attempts")
    events: Mapped[list["CallEvent"]] = relationship(back_populates="call_attempt")


class CallEvent(Base):
    """Fine-grained event/audit log for a call attempt (webhooks, state transitions)."""

    __tablename__ = "call_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    call_attempt_id: Mapped[int | None] = mapped_column(ForeignKey("call_attempts.id"))
    provider_call_sid: Mapped[str | None] = mapped_column(String(128), index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    call_attempt: Mapped["CallAttempt | None"] = relationship(back_populates="events")


class DailySession(Base):
    """Scheduler state-transition log; also drives restart recovery."""

    __tablename__ = "daily_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    entered_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    exited_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    report_generated: Mapped[bool] = mapped_column(Boolean, default=False)


class AgentSetting(Base):
    """Key/value runtime settings (campaign status, last-processed pointer, etc.)."""

    __tablename__ = "agent_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DoNotCall(Base):
    """Explicit DNC registry — survives sheet re-imports independent of Consumer.do_not_call."""

    __tablename__ = "do_not_call"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consumer_no: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    mobile_number: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32), default="call")
    requested_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    """Generic audit trail for sensitive/administrative actions."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    details_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
