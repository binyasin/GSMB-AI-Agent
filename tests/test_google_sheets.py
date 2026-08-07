from __future__ import annotations

import pytest

from app.google_sheets import GoogleSheetRepository, SheetValidationError
from app.schemas import AlreadyPaidStatus
from tests.conftest import FakeWorksheet, make_sample_worksheet


def test_reads_rows_by_header_name_not_position():
    ws = make_sample_worksheet()
    repo = GoogleSheetRepository(ws)
    records = repo.read_rows()
    by_no = {r.consumer_no: r for r in records}
    assert by_no["CN-001"].consumer_name == "Ali Raza"
    assert by_no["CN-001"].outstanding_amount == 12500.0
    assert by_no["CN-001"].installment_eligible is True


def test_validate_required_columns_passes_for_full_schema():
    ws = make_sample_worksheet()
    GoogleSheetRepository(ws).validate_required_columns()  # should not raise


def test_validate_required_columns_raises_when_missing():
    ws = FakeWorksheet(headers=["Consumer No", "Consumer Name"])
    with pytest.raises(SheetValidationError) as exc:
        GoogleSheetRepository(ws).validate_required_columns()
    assert "Mobile Number" in str(exc.value)
    assert "Outstanding Amount" in str(exc.value)


def test_missing_optional_columns_handled_gracefully():
    # Only the required minimum columns are present; everything else should
    # default rather than raise.
    ws = FakeWorksheet(
        headers=["Consumer No", "Consumer Name", "Mobile Number", "Outstanding Amount", "Due Date", "Call Status", "Last Call Date"],
        rows=[["CN-100", "Test User", "03001112233", "1000", "2026-08-15", "PENDING", ""]],
    )
    records = GoogleSheetRepository(ws).read_rows()
    assert len(records) == 1
    assert records[0].scheme_available is False
    assert records[0].installment_details is None


def test_already_paid_customer_claims_paid_parsed():
    ws = FakeWorksheet(
        headers=["Consumer No", "Consumer Name", "Mobile Number", "Outstanding Amount", "Due Date", "Call Status", "Last Call Date", "Already Paid"],
        rows=[["CN-200", "Test User", "03001112233", "1000", "2026-08-15", "PENDING", "", "CUSTOMER_CLAIMS_PAID"]],
    )
    records = GoogleSheetRepository(ws).read_rows()
    assert records[0].already_paid == AlreadyPaidStatus.CUSTOMER_CLAIMS_PAID


def test_blank_consumer_no_rows_skipped():
    ws = FakeWorksheet(
        headers=["Consumer No", "Consumer Name", "Mobile Number", "Outstanding Amount", "Due Date", "Call Status", "Last Call Date"],
        rows=[["", "Blank Row", "", "", "", "", ""]],
    )
    assert GoogleSheetRepository(ws).read_rows() == []


def test_find_row_number_by_consumer_no():
    ws = make_sample_worksheet()
    repo = GoogleSheetRepository(ws)
    assert repo.find_row_number("CN-003") == 4  # header=1, CN-001=2, CN-002=3, CN-003=4
    assert repo.find_row_number("CN-999") is None


def test_update_row_by_consumer_no_writes_correct_cells():
    ws = make_sample_worksheet()
    repo = GoogleSheetRepository(ws)
    repo.update_row_by_consumer_no("CN-001", {"Call Status": "COMPLETED", "Call Outcome": "PROMISE_TO_PAY"})

    records = repo.read_rows()
    updated = next(r for r in records if r.consumer_no == "CN-001")
    assert updated.call_status == "COMPLETED"
    assert updated.call_outcome == "PROMISE_TO_PAY"
    # Untouched fields on the same row must survive the update.
    assert updated.consumer_name == "Ali Raza"


def test_update_row_unknown_consumer_raises():
    ws = make_sample_worksheet()
    with pytest.raises(SheetValidationError):
        GoogleSheetRepository(ws).update_row_by_consumer_no("CN-DOES-NOT-EXIST", {"Call Status": "X"})


def test_update_row_unknown_column_raises():
    ws = make_sample_worksheet()
    with pytest.raises(SheetValidationError):
        GoogleSheetRepository(ws).update_row_by_consumer_no("CN-001", {"Not A Real Column": "X"})
