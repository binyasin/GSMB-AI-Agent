from __future__ import annotations

import pytest

from app.utils import is_valid_pakistani_mobile, normalize_pakistani_mobile


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("03001234567", "+923001234567"),
        ("0300 123 4567", "+923001234567"),
        ("0300-123-4567", "+923001234567"),
        ("+923001234567", "+923001234567"),
        ("00923001234567", "+923001234567"),
        ("923001234567", "+923001234567"),
        ("3001234567", "+923001234567"),
    ],
)
def test_normalize_valid_formats(raw, expected):
    assert normalize_pakistani_mobile(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not-a-number",
        "12345",
        "02112345678",  # landline (02x), not mobile
        "030012345",  # too short
        "030012345678",  # too long
        "+14155552671",  # US number
    ],
)
def test_normalize_invalid_formats_return_none(raw):
    assert normalize_pakistani_mobile(raw) is None


def test_is_valid_pakistani_mobile_matches_normalize():
    assert is_valid_pakistani_mobile("03001234567") is True
    assert is_valid_pakistani_mobile("invalid") is False
