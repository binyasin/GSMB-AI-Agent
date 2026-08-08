from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.schemas import ALL_SHEET_COLUMNS


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _clear_settings_cache(monkeypatch):
    """Isolate tests from whatever real .env happens to exist on disk.

    Settings.model_config normally points at ".env" so the live app picks
    up real credentials automatically -- but that means, once a real .env
    exists (e.g. after wiring up Twilio/Google Sheets), every test would
    silently inherit those real credentials instead of the field defaults
    a test expects, and some would start making live network calls. Each
    test gets pure field-defaults-plus-whatever-it-explicitly-monkeypatches.
    """
    from app.config import Settings, get_settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()


class FakeWorksheet:
    """Minimal in-memory stand-in for a gspread.Worksheet.

    Stores rows as list[list[str]] with row 0 as the header row, matching
    gspread's get_all_values()/update_cell() surface closely enough for
    GoogleSheetRepository to be fully exercised without any network call.
    """

    def __init__(self, headers: list[str], rows: list[list[str]] | None = None):
        self._data: list[list[str]] = [list(headers)] + [list(r) for r in (rows or [])]

    def get_all_values(self) -> list[list[str]]:
        return [list(r) for r in self._data]

    def update_cell(self, row: int, col: int, value: str) -> None:
        # gspread rows/cols are 1-indexed
        r, c = row - 1, col - 1
        while len(self._data) <= r:
            self._data.append([""] * len(self._data[0]))
        line = self._data[r]
        while len(line) <= c:
            line.append("")
        line[c] = value

    def append_row(self, values: list[str]) -> None:
        self._data.append(list(values))


def make_sample_worksheet() -> FakeWorksheet:
    """Uses GSM Brothers' real sheet column headers (app.schemas.ALL_SHEET_COLUMNS).

    There's no dedicated Already Paid / Do Not Call column in the real
    sheet — CN-002/CN-003 below exercise the Call Status text heuristic
    (see app/schemas.py `_derive_already_paid` / `_derive_do_not_call`).
    """
    headers = ALL_SHEET_COLUMNS
    rows = [
        _row(
            headers,
            {
                "Consumer No.": "CN-001",
                "Name": "Ali Raza",
                "Consumer Phone Number": "03001234567",
                "Address": "House 1, Karachi",
                "DUES": "12500",
                "Rate Tariff": "A1",
                "Scheme eligibility": "3 installments of Rs. 4167",
                "Call Status": "PENDING",
            },
        ),
        _row(
            headers,
            {
                "Consumer No.": "CN-002",
                "Name": "Sana Tariq",
                "Consumer Phone Number": "03211112222",
                "DUES": "5000",
                "Call Status": "Already Paid",
            },
        ),
        _row(
            headers,
            {
                "Consumer No.": "CN-003",
                "Name": "Bilal Khan",
                "Consumer Phone Number": "03451234567",
                "DUES": "8000",
                "Call Status": "Do Not Call",
            },
        ),
        _row(
            headers,
            {
                "Consumer No.": "CN-004",
                "Name": "Nadia Yousuf",
                "Consumer Phone Number": "not-a-number",
                "DUES": "3000",
                "Call Status": "PENDING",
            },
        ),
        _row(
            headers,
            {
                "Consumer No.": "CN-005",
                "Name": "Usman Ghani",
                "Consumer Phone Number": "03009998877",
                "DUES": "15000",
                "Call Status": "COMPLETED",
            },
        ),
    ]
    return FakeWorksheet(headers, rows)


def _row(headers: list[str], values: dict[str, str]) -> list[str]:
    return [values.get(h, "") for h in headers]
