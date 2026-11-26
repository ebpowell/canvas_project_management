"""Google Sheets client using gspread for idempotent tabular sync of assignments and discrepancies."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Union
import gspread

from canvas_tracker.config import StudentConfig, StudentEntryConfig
from canvas_tracker.normalizer import NormalizedAssignment
from canvas_tracker.reconciler import UnifiedAssignment

logger = logging.getLogger(__name__)

LEGACY_HEADERS = ["Course", "Assignment", "Start Date", "Due Date", "Status", "Weight/Pts", "Link"]


class GoogleSheetsSync:
    """Manages connection and idempotent tab synchronization for Google Sheets."""

    def __init__(self, service_account_path: Path, spreadsheet_name_or_id: str):
        self.service_account_path = Path(service_account_path)
        self.spreadsheet_name_or_id = spreadsheet_name_or_id
        self._client: Optional[gspread.Client] = None
        self._sheet: Optional[gspread.Spreadsheet] = None

    def connect(self) -> bool:
        """Authenticate with Google Sheets API."""
        if not self.service_account_path.exists():
            logger.warning(
                "Google Service Account key not found at %s. Google Sheets sync will be skipped.",
                self.service_account_path,
            )
            return False

        try:
            self._client = gspread.service_account(filename=str(self.service_account_path))
            try:
                self._sheet = self._client.open_by_key(self.spreadsheet_name_or_id)
            except Exception:
                self._sheet = self._client.open(self.spreadsheet_name_or_id)
            return True
        except Exception as e:
            logger.error("Failed to connect to Google Sheets (%s): %s", self.spreadsheet_name_or_id, e)
            return False

    def _get_or_create_tab(self, title: str, rows: int = 200, cols: int = 10) -> Optional[gspread.Worksheet]:
        if not self._sheet:
            return None
        try:
            return self._sheet.worksheet(title)
        except gspread.WorksheetNotFound:
            return self._sheet.add_worksheet(title=title, rows=rows, cols=cols)

    def sync_student_tab(self, tab_name: str, assignments: List[NormalizedAssignment]) -> bool:
        """Legacy v1 sync method for NormalizedAssignment."""
        if not self._client and not self.connect():
            return False

        try:
            ws = self._get_or_create_tab(tab_name, rows=len(assignments) + 10, cols=len(LEGACY_HEADERS) + 2)
            if not ws:
                return False

            rows = [LEGACY_HEADERS]
            for a in sorted(assignments, key=lambda x: (x.due_date, x.course)):
                rows.append(a.to_sheet_row())

            ws.clear()
            ws.update(range_name="A1", values=rows, value_input_option="USER_ENTERED")
            logger.info("Successfully updated tab '%s' with %d assignments.", tab_name, len(assignments))
            return True
        except Exception as e:
            logger.error("Error writing to tab '%s': %s", tab_name, e)
            return False

    def sync_unified_assignments(
        self,
        student: StudentEntryConfig | StudentConfig,
        assignments: List[UnifiedAssignment],
    ) -> bool:
        """
        Synchronize unified assignments into both Raw_Data_<Student> and
        Discrepancies_<Student> worksheets.
        """
        if not self._client and not self.connect():
            return False

        entry = student.to_student_entry() if isinstance(student, StudentConfig) else student

        raw_tab = entry.outputs.google_sheets.raw_data_tab
        disc_tab = entry.outputs.google_sheets.discrepancies_tab

        raw_headers = [
            "Course",
            "Assignment",
            "Start Date",
            "Due Date",
            "Status",
            "Weight / Pts",
            "Source",
            "Risk Score",
            "Urgency Tier",
            "Link",
        ]
        disc_headers = [
            "Course",
            "Assignment",
            "Aspen Due Date",
            "Aspen Score",
            "Risk Score",
            "Recommended Action",
            "LLM Teacher Inquiry Draft",
        ]

        try:
            # 1. Update Raw_Data tab
            raw_ws = self._get_or_create_tab(raw_tab, rows=max(200, len(assignments) + 10), cols=10)
            if raw_ws:
                sorted_items = sorted(assignments, key=lambda a: (a.due_date, -a.risk_score))
                raw_rows = [raw_headers]
                for a in sorted_items:
                    raw_rows.append(a.to_sheet_row())
                raw_ws.clear()
                raw_ws.update(range_name="A1", values=raw_rows, value_input_option="USER_ENTERED")

            # 2. Update Discrepancies tab
            disc_ws = self._get_or_create_tab(disc_tab, rows=100, cols=7)
            if disc_ws:
                discrepancies = [a for a in assignments if a.is_discrepancy]
                disc_rows = [disc_headers]
                for d in discrepancies:
                    disc_rows.append(d.to_discrepancy_row())
                disc_ws.clear()
                disc_ws.update(range_name="A1", values=disc_rows, value_input_option="USER_ENTERED")

            logger.info(
                "Synced %d total rows and %d discrepancies to Google Sheets for '%s'",
                len(assignments),
                len([a for a in assignments if a.is_discrepancy]),
                entry.id,
            )
            return True
        except Exception as exc:
            logger.error("Google Sheets sync failed for '%s': %s", entry.id, exc)
            return False


# Top-level standalone function for direct pipeline calls
def sync_to_google_sheets(
    student: StudentEntryConfig,
    assignments: List[UnifiedAssignment],
    spreadsheet_id: str,
    service_account_file: Path,
) -> None:
    """Wrapper function preserving canvas_scaper.py signature."""
    client = GoogleSheetsSync(
        service_account_path=service_account_file,
        spreadsheet_name_or_id=spreadsheet_id,
    )
    client.sync_unified_assignments(student=student, assignments=assignments)
