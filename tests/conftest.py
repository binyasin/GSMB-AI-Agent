from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.schemas import ALL_SHEET_COLUMNS


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    from app.config import get_settings

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
    headers = ALL_SHEET_COLUMNS
    today = dt.date.today()
    rows = [
        _row(
            headers,
            {
                "Consumer No": "CN-001",
                "Consumer Name": "Ali Raza",
                "Father Name": "Ahmed Raza",
                "Mobile Number": "03001234567",
                "Address": "House 1, Karachi",
                "Outstanding Amount": "12500",
                "Current Bill": "2500",
                "Arrears": "10000",
                "Due Date": today.isoformat(),
                "Tariff": "A1",
                "Installment Eligible": "YES",
                "Installment Details": "3 installments of Rs. 4167",
                "Scheme Available": "YES",
                "Scheme Description": "Standard K-Electric relief scheme",
                "Status": "Active",
                "Already Paid": "NO",
                "Call Status": "PENDING",
                "Do Not Call": "NO",
                "Human Follow-up": "NO",
            },
        ),
        _row(
            headers,
            {
                "Consumer No": "CN-002",
                "Consumer Name": "Sana Tariq",
                "Mobile Number": "03211112222",
                "Outstanding Amount": "5000",
                "Due Date": today.isoformat(),
                "Call Status": "PENDING",
                "Already Paid": "YES",
                "Do Not Call": "NO",
            },
        ),
        _row(
            headers,
            {
                "Consumer No": "CN-003",
                "Consumer Name": "Bilal Khan",
                "Mobile Number": "03451234567",
                "Outstanding Amount": "8000",
                "Due Date": today.isoformat(),
                "Call Status": "PENDING",
                "Do Not Call": "YES",
            },
        ),
        _row(
            headers,
            {
                "Consumer No": "CN-004",
                "Consumer Name": "Nadia Yousuf",
                "Mobile Number": "not-a-number",
                "Outstanding Amount": "3000",
                "Due Date": today.isoformat(),
                "Call Status": "PENDING",
            },
        ),
        _row(
            headers,
            {
                "Consumer No": "CN-005",
                "Consumer Name": "Usman Ghani",
                "Mobile Number": "03009998877",
                "Outstanding Amount": "15000",
                "Due Date": today.isoformat(),
                "Call Status": "COMPLETED",
            },
        ),
    ]
    return FakeWorksheet(headers, rows)


def _row(headers: list[str], values: dict[str, str]) -> list[str]:
    return [values.get(h, "") for h in headers]
