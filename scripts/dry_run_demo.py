"""Standalone dry-run walkthrough (spec Sec.43, Sec.18 Phase 18/19).

Exercises the full pipeline — fixture Google Sheet -> queue -> scheduler
window check -> simulated call -> DB write -> simulated sheet write -> daily
report — with **zero external credentials** (no Google, Twilio, or AI
account needed) and **no telecom cost**. This is exactly what DRY_RUN=true
does inside the real app; this script just drives it standalone and prints
every step so the wiring can be inspected end-to-end.

Run:  python scripts/dry_run_demo.py
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("TEST_MODE", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.config import get_settings  # noqa: E402
from app.database import SessionLocal, engine, init_db  # noqa: E402
from app.google_sheets import GoogleSheetRepository  # noqa: E402
from app.models import Base  # noqa: E402
from app.calling_agent import process_next_consumer  # noqa: E402
from app.queue_manager import build_daily_queue  # noqa: E402
from app.reports import compute_daily_report  # noqa: E402
from app.scheduler import compute_state  # noqa: E402
from app.schemas import ALL_SHEET_COLUMNS  # noqa: E402
from app.utils import now_local  # noqa: E402


class InMemoryWorksheet:
    """Same minimal surface as gspread.Worksheet — see app/google_sheets.py WorksheetLike."""

    def __init__(self, headers, rows):
        self._data = [list(headers)] + [list(r) for r in rows]

    def get_all_values(self):
        return [list(r) for r in self._data]

    def update_cell(self, row, col, value):
        r, c = row - 1, col - 1
        while len(self._data) <= r:
            self._data.append([""] * len(self._data[0]))
        line = self._data[r]
        while len(line) <= c:
            line.append("")
        line[c] = value

    def append_row(self, values):
        self._data.append(list(values))


def _row(headers, values):
    return [values.get(h, "") for h in headers]


def build_fixture_sheet(today: dt.date) -> InMemoryWorksheet:
    """Fixture using GSM Brothers' real sheet column headers (app.schemas.ALL_SHEET_COLUMNS).

    There's no dedicated Already Paid / Do Not Call column in the real
    sheet -- DEMO-003 below is excluded via the Call Status text heuristic
    (see app/schemas.py `_derive_already_paid`).
    """
    headers = ALL_SHEET_COLUMNS
    rows = [
        _row(headers, {
            "Consumer No.": "DEMO-001", "Name": "Fatima Sheikh",
            "Consumer Phone Number": "03211234567", "Address": "Block 4, Gulshan-e-Iqbal, Karachi",
            "DUES": "18500", "Rate Tariff": "A1",
            "Scheme eligibility": "3 installments of Rs. 6167",
            "Call Status": "PENDING",
        }),
        _row(headers, {
            "Consumer No.": "DEMO-002", "Name": "Kamran Ahmed", "Consumer Phone Number": "03451112233",
            "DUES": "9000", "Call Status": "PENDING",
        }),
        _row(headers, {
            "Consumer No.": "DEMO-003", "Name": "Rukhsana Bibi", "Consumer Phone Number": "03001239876",
            "DUES": "5000", "Call Status": "Already Paid",  # -> should be skipped
        }),
    ]
    return InMemoryWorksheet(headers, rows)


def main() -> None:
    settings = get_settings()
    print(f"TEST_MODE={settings.test_mode}  DRY_RUN={settings.dry_run}  TIMEZONE={settings.timezone}")
    print()

    init_db()
    session = SessionLocal()
    today = now_local().date()

    print("== Step 1: scheduler window check ==")
    state = compute_state(now_local(), settings)
    print(f"Current Pakistan time state: {state.value}")
    print("(Informational only -- the dry run processes the queue regardless of window,")
    print(" so you can exercise the pipeline at any time of day.)\n")

    print("== Step 2: read fixture Google Sheet ==")
    sheet = GoogleSheetRepository(build_fixture_sheet(today))
    sheet.validate_required_columns()
    records = sheet.read_rows()
    print(f"Read {len(records)} row(s) from the fixture sheet.\n")

    print("== Step 3: build today's eligible queue ==")
    queue = build_daily_queue(session, records, job_date=today)
    print(f"{len(queue)} consumer(s) eligible today: {[c.consumer_no for c in queue]}")
    print("(DEMO-003 correctly excluded: Already Paid=YES)\n")

    print("== Step 4: process each consumer (simulated call, DRY_RUN=true) ==")
    processed = []
    while True:
        attempt = process_next_consumer(session, sheet_repo=sheet, job_date=today)
        if attempt is None:
            break
        processed.append(attempt)
        print(f"  consumer_no={attempt.consumer_no}  result={attempt.result}  outcome={attempt.call_outcome}  "
              f"duration={attempt.call_duration_seconds}s  sheet_synced={attempt.sheet_synced}")

    print(f"\nProcessed {len(processed)} call(s).\n")

    print("== Step 5: verify DB state ==")
    for attempt in processed:
        from app.models import Consumer

        consumer = session.query(Consumer).filter_by(consumer_no=attempt.consumer_no).one()
        print(f"  {consumer.consumer_no}: call_status={consumer.call_status} call_attempt={consumer.call_attempt} "
              f"promise_to_pay_date={consumer.promise_to_pay_date} human_followup={consumer.human_followup}")
        if consumer.agent_notes:
            print(f"      agent_notes: {consumer.agent_notes!r}  (vague timing captured verbatim, not guessed into a date)")

    print("\n== Step 6: verify simulated sheet write-back ==")
    sheet_values = sheet.worksheet.get_all_values()
    headers = sheet_values[0]
    for row in sheet_values[1:]:
        row_map = dict(zip(headers, row))
        if row_map.get("Consumer No.") in {a.consumer_no for a in processed}:
            print(f"  {row_map['Consumer No.']}: Call Status={row_map.get('Call Status')!r} "
                  f"Call out come={row_map.get('Call out come')!r} Transcript length={len(row_map.get('Transcript') or '')}")

    print("\n== Step 7: generate daily report from DB ==")
    report = compute_daily_report(session, today)
    print(report.model_dump_json(indent=2))

    session.close()
    print("\nDry run complete. No telephony call was placed and no external credential was required.")


if __name__ == "__main__":
    main()
