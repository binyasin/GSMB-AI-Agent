"""Telephony provider abstraction (spec Sec.37).

`TelephonyProvider` is the only thing `calling_agent.py` talks to for
placing/controlling calls, so a Telnyx (or other) implementation can be
added later without touching call orchestration logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class CallHandle:
    provider_call_sid: str
    status: str


class TelephonyProvider(ABC):
    """Abstract outbound-call provider."""

    @abstractmethod
    def make_call(self, to_number: str, voice_webhook_url: str, status_webhook_url: str) -> CallHandle:
        """Place an outbound call. Returns immediately with the provider's call SID;
        the call's actual progress arrives via status webhooks."""

    @abstractmethod
    def hangup(self, call_sid: str) -> None:
        """End an in-progress call."""

    @abstractmethod
    def transfer_call(self, call_sid: str, human_number: str) -> None:
        """Transfer a live call to a human agent number (spec Sec.30)."""

    @abstractmethod
    def get_call_status(self, call_sid: str) -> str:
        """Query the provider's current status for a call SID."""

    @abstractmethod
    def get_recording_url(self, call_sid: str) -> str | None:
        """URL of the call recording, or None if recording is disabled/unavailable."""

    @abstractmethod
    def verify_webhook_signature(self, url: str, params: dict, signature: str) -> bool:
        """Validate that an inbound webhook actually came from the provider
        (spec Sec.38: "Do not trust arbitrary incoming webhook requests.")."""
