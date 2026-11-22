"""Google Sheets client using gspread for idempotent tabular sync."""

import logging
from pathlib import Path
from typing import List, Optional
import gspread
from google.oauth2.service_account import Credentials

from canvas_tracker.normalizer import NormalizedAssignment

logger = logging.getLogger(__name__)

HEADERS = ["Course", "Assignment", "Start Date", "Due Date", "Status", "Weight/Pts", "Link"]
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


class GoogleSheetsSync:
    """Manages connection and idempotent tab synchronization for Google Sheets."""

    def __init__(self, service_account_path: Path, spreadsheet_name_or_id: str):
        self.service_account_path = service_account_path
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
            creds = Credentials.from_service_account_file(
                str(self.service_account_path), scopes=SCOPES
            )
            self._client = gspread.authorize(creds)
            # Try by key or by title
            try:
                self._sheet = self._client.open_by_key(self.spreadsheet_name_or_id)
            except Exception:
                self._sheet = self._client.open(self.spreadsheet_name_or_id)
            logger.info("Successfully connected to Google Spreadsheet: %s", self._sheet.title)
            return True
        except Exception as e:
            logger.error("Failed to authenticate or open Google Sheet '%s': %s", self.spreadsheet_name_or_id, e)
            return False

    def sync_student_tab(self, tab_name: str, assignments: List[NormalizedAssignment]) -> bool:
        """
        Idempotently create or update the student's tab with normalized assignment rows.
        Overwrites values in-place so formula references in other tabs remain unbroken.
        """
        if not self._sheet:
            if not self.connect():
                return False

        try:
            # Find worksheet or create if not present
            try:
                worksheet = self._sheet.worksheet(tab_name)
            except gspread.WorksheetNotFound:
                logger.info("Worksheet '%s' not found. Creating new tab...", tab_name)
                worksheet = self._sheet.add_worksheet(title=tab_name, rows=100, cols=len(HEADERS))

            # Build matrix: headers + sorted assignments
            rows = [HEADERS]
            # Sort by due date ascending
            sorted_assignments = sorted(assignments, key=lambda x: (x.due_date, x.course))
            for item in sorted_assignments:
                rows.append(item.to_sheet_row())

            # Clear previous contents and update
            worksheet.clear()
            worksheet.update(values=rows, range_name="A1")

            # Format header row bold
            worksheet.format("A1:G1", {"textFormat": {"bold": True}})

            logger.info("Successfully synced %d assignments to Google Sheet tab '%s'.", len(assignments), tab_name)
            return True

        except Exception as e:
            logger.error("Error updating tab '%s': %s", tab_name, e)
            return False
