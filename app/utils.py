"""Small shared helpers: phone validation/normalization and timezone utilities."""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

from app.config import get_settings

_DIGITS_RE = re.compile(r"\D+")


def normalize_pakistani_mobile(raw: str | None) -> str | None:
    """Normalize a Pakistani mobile number to E.164 (+923XXXXXXXXX).

    Accepts 03XXXXXXXXX, 3XXXXXXXXX, +923XXXXXXXXX, 00923XXXXXXXXX,
    923XXXXXXXXX (with or without spaces/dashes). Returns None if the
    number doesn't match a structurally valid Pakistani mobile number.
    """
    if not raw:
        return None
    digits = _DIGITS_RE.sub("", raw)

    if digits.startswith("0092"):
        digits = digits[4:]
    elif digits.startswith("92"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = digits[1:]

    # `digits` should now be 10 digits: 3XXXXXXXXX
    if len(digits) != 10 or not digits.startswith("3"):
        return None

    return f"+92{digits}"


def is_valid_pakistani_mobile(raw: str | None) -> bool:
    return normalize_pakistani_mobile(raw) is not None


def get_timezone() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def now_local() -> dt.datetime:
    return dt.datetime.now(get_timezone())


def today_local() -> dt.date:
    return now_local().date()
