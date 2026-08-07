"""Google Sheets integration.

Column access is always by header name, never by fixed position (spec
Sec.5: "The code must not depend blindly on column positions"). The
`GoogleSheetRepository` logic is separated from *how* a worksheet handle is
obtained (`open_worksheet`, which needs real service-account credentials)
so the read/validate/update logic can be unit-tested against a fake
in-memory worksheet with zero external dependencies.
"""

from __future__ import annotations

import json
import logging
from typing import Protocol

from app.config import ConfigurationError, Settings, get_settings
from app.schemas import ALL_SHEET_COLUMNS, REQUIRED_SHEET_COLUMNS, ConsumerRecord

logger = logging.getLogger("calls")


class SheetValidationError(RuntimeError):
    """Raised when the worksheet's headers don't satisfy the minimum contract."""


class WorksheetLike(Protocol):
    """The minimal surface `GoogleSheetRepository` needs from a worksheet.

    Satisfied by a real `gspread.Worksheet` and by `tests` fakes.
    """

    def get_all_values(self) -> list[list[str]]: ...
    def update_cell(self, row: int, col: int, value: str) -> None: ...
    def append_row(self, values: list[str]) -> None: ...


def _load_google_credentials(settings: Settings):
    """Build `google.oauth2.service_account.Credentials` from settings.

    Requires GOOGLE_SERVICE_ACCOUNT_JSON (raw JSON string) or
    GOOGLE_SERVICE_ACCOUNT_FILE (path to the JSON key file).
    """
    from google.oauth2.service_account import Credentials

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    if settings.google_service_account_json:
        info = json.loads(settings.google_service_account_json)
        return Credentials.from_service_account_info(info, scopes=scopes)
    if settings.google_service_account_file:
        return Credentials.from_service_account_file(settings.google_service_account_file, scopes=scopes)
    raise ConfigurationError(
        "No Google service account credentials configured "
        "(GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE)."
    )


def open_worksheet(settings: Settings | None = None):
    """Open the configured worksheet via a real gspread client.

    Raises ConfigurationError if credentials/spreadsheet ID are missing.
    This is the only function in this module that talks to the network —
    everything else operates on a `WorksheetLike` handle.
    """
    settings = settings or get_settings()
    settings.require_google_sheets()

    import gspread

    credentials = _load_google_credentials(settings)
    client = gspread.authorize(credentials)
    spreadsheet = client.open_by_key(settings.google_spreadsheet_id)
    return spreadsheet.worksheet(settings.google_worksheet_name)


class GoogleSheetRepository:
    """Header-name-based read/write access to the consumer queue worksheet."""

    def __init__(self, worksheet: WorksheetLike):
        self.worksheet = worksheet

    # -- schema -------------------------------------------------------
    def get_headers(self) -> list[str]:
        values = self.worksheet.get_all_values()
        if not values:
            return []
        return [h.strip() for h in values[0]]

    def validate_required_columns(self) -> None:
        headers = self.get_headers()
        missing = [c for c in REQUIRED_SHEET_COLUMNS if c not in headers]
        if missing:
            raise SheetValidationError(
                "Google Sheet is missing required column(s): " + ", ".join(missing)
            )

    def unknown_columns(self) -> list[str]:
        """Columns present in the sheet but not part of the recognized schema (informational)."""
        headers = self.get_headers()
        return [h for h in headers if h and h not in ALL_SHEET_COLUMNS]

    # -- read -----------------------------------------------------------
    def read_rows(self) -> list[ConsumerRecord]:
        """Read every data row into a validated ConsumerRecord, skipping blank rows."""
        self.validate_required_columns()
        values = self.worksheet.get_all_values()
        if len(values) < 2:
            return []
        headers = [h.strip() for h in values[0]]
        records = []
        for raw_row in values[1:]:
            row = dict(zip(headers, raw_row + [""] * (len(headers) - len(raw_row))))
            if not row.get("Consumer No", "").strip():
                continue
            try:
                records.append(ConsumerRecord.from_row(row))
            except Exception:
                logger.exception("skipping unparsable sheet row for consumer_no=%s", row.get("Consumer No"))
        return records

    def find_row_number(self, consumer_no: str) -> int | None:
        values = self.worksheet.get_all_values()
        if not values:
            return None
        headers = [h.strip() for h in values[0]]
        if "Consumer No" not in headers:
            return None
        col_idx = headers.index("Consumer No")
        for i, raw_row in enumerate(values[1:], start=2):
            if col_idx < len(raw_row) and raw_row[col_idx].strip() == consumer_no:
                return i
        return None

    # -- write ------------------------------------------------------------
    def update_row_by_consumer_no(self, consumer_no: str, updates: dict[str, str]) -> None:
        """Write `updates` (header_name -> value) to the row matching consumer_no.

        Raises SheetValidationError if consumer_no isn't found or a target
        column doesn't exist in the sheet — silently dropping a requested
        update would violate spec Sec.33 ("do not lose the result").
        """
        headers = self.get_headers()
        unknown_targets = [h for h in updates if h not in headers]
        if unknown_targets:
            raise SheetValidationError(
                "Cannot update column(s) not present in the sheet: " + ", ".join(unknown_targets)
            )
        row_number = self.find_row_number(consumer_no)
        if row_number is None:
            raise SheetValidationError(f"Consumer No '{consumer_no}' not found in sheet")

        for header, value in updates.items():
            col = headers.index(header) + 1
            self.worksheet.update_cell(row_number, col, value)
