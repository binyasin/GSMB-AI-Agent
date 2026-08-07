"""Security-relevant helpers, gathered in one place for discoverability.

The actual implementations live where they naturally belong (webhook
signature verification is provider-specific, so it's a method on
`TelephonyProvider`; do-not-call enforcement is an eligibility rule, so
it's part of `queue_manager`) — this module re-exports them rather than
duplicating logic, so `grep`-ing for "security" or reading this file first
still finds the real code paths.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DoNotCall
from app.queue_manager import skip_reason
from app.telephony.base import TelephonyProvider

__all__ = ["verify_webhook_signature", "is_on_do_not_call_registry", "DoNotCall", "skip_reason"]


def verify_webhook_signature(provider: TelephonyProvider, url: str, params: dict, signature: str) -> bool:
    """Spec Sec.38: never trust an inbound webhook without verifying it came
    from the telephony provider. See app/webhooks/voice.py:verify_and_extract
    for where this is actually enforced on every webhook route."""
    return provider.verify_webhook_signature(url, params, signature)


def is_on_do_not_call_registry(session: Session, consumer_no: str) -> bool:
    """Spec Sec.31: the local DNC registry is authoritative even if a sheet
    edit reverts the Do Not Call column — see queue_manager.skip_reason,
    which enforces this same check during queue building."""
    return session.scalar(select(DoNotCall).where(DoNotCall.consumer_no == consumer_no)) is not None
