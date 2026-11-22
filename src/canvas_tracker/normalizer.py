"""Data validation and date normalization for Canvas assignments."""

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field, field_validator


class NormalizedAssignment(BaseModel):
    """Standardized assignment structure ready for Google Sheets and Home Assistant."""

    id: str
    course: str
    name: str
    start_date: str  # YYYY-MM-DD
    due_date: str    # YYYY-MM-DD
    status: str      # Missing, Late, Submitted, Graded, Upcoming
    points: str      # e.g., "100" or "Unweighted"
    link: str

    def to_sheet_row(self) -> list:
        """Return formatted row for Google Sheets."""
        return [
            self.course,
            self.name,
            self.start_date,
            self.due_date,
            self.status,
            self.points,
            self.link,
        ]

    def to_mqtt_item(self) -> Dict[str, Any]:
        """Return structured dict for Home Assistant ApexCharts / Lovelace cards."""
        return {
            "id": self.id,
            "course": self.course,
            "name": self.name,
            "start": self.start_date,
            "end": self.due_date,
            "status": self.status,
            "points": self.points,
            "link": self.link,
        }


def parse_iso_date(value: Optional[str]) -> Optional[date]:
    """Safely parse an ISO-8601 date/timestamp string to a date object."""
    if not value or not str(value).strip():
        return None
    val_str = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(val_str)
        return dt.date()
    except (ValueError, TypeError):
        # Fallback for simple YYYY-MM-DD
        try:
            return datetime.strptime(val_str[:10], "%Y-%m-%d").date()
        except Exception:
            return None


def calculate_start_date(unlock_date: Optional[date], due_date: Optional[date], today: Optional[date] = None) -> date:
    """
    Calculate normalized start_date in YYYY-MM-DD:
    Uses unlock_at if available.
    Falls back to due_at - 7 days (or today's date if due_at is within 7 days of today or overdue).
    """
    current_day = today or datetime.now(timezone.utc).date()

    if unlock_date is not None:
        return unlock_date

    if due_date is None:
        return current_day

    seven_days_prior = due_date - timedelta(days=7)
    if seven_days_prior < current_day:
        return current_day
    return seven_days_prior


def determine_status(
    raw_status: Optional[str],
    submission_data: Optional[Dict[str, Any]],
    due_date: Optional[date],
    today: Optional[date] = None,
) -> str:
    """
    Determine standardized status: Missing, Late, Submitted, Graded, Upcoming.
    """
    current_day = today or datetime.now(timezone.utc).date()
    sub = submission_data or {}

    workflow_state = str(sub.get("workflow_state", "")).lower()
    has_submission = bool(sub.get("submitted_at") or workflow_state in ("submitted", "graded", "pending_review"))
    is_graded = workflow_state == "graded" or sub.get("grade") is not None
    is_missing = bool(sub.get("missing", False))
    is_late = bool(sub.get("late", False))

    if raw_status:
        cleaned_raw = raw_status.strip().capitalize()
        if cleaned_raw in ("Missing", "Late", "Submitted", "Graded", "Upcoming"):
            return cleaned_raw

    if is_graded:
        return "Graded"

    if has_submission:
        return "Late" if is_late else "Submitted"

    if is_missing:
        return "Missing"

    if due_date is not None and due_date < current_day:
        return "Missing"

    if is_late:
        return "Late"

    return "Upcoming"


def normalize_assignment(raw: Dict[str, Any], today: Optional[date] = None) -> NormalizedAssignment:
    """Normalize a raw assignment record from canvas-scraper or Canvas API."""
    current_day = today or datetime.now(timezone.utc).date()

    raw_id = str(raw.get("id") or raw.get("assignment_id") or "0")
    name = str(raw.get("name") or raw.get("title") or "Untitled Assignment").strip()
    course = str(raw.get("course") or raw.get("course_name") or "General").strip()

    due_raw = raw.get("due_at") or raw.get("due_date")
    unlock_raw = raw.get("unlock_at") or raw.get("unlock_date")

    due_parsed = parse_iso_date(due_raw)
    unlock_parsed = parse_iso_date(unlock_raw)

    due_date_obj = due_parsed or (current_day + timedelta(days=7))
    start_date_obj = calculate_start_date(unlock_parsed, due_parsed, today=current_day)

    # Ensure start_date does not exceed due_date
    if start_date_obj > due_date_obj:
        start_date_obj = due_date_obj

    submission = raw.get("submission") if isinstance(raw.get("submission"), dict) else {}
    status = determine_status(
        raw_status=raw.get("status"),
        submission_data=submission,
        due_date=due_parsed,
        today=current_day,
    )

    pts = raw.get("points_possible")
    points_str = f"{pts:g}" if isinstance(pts, (int, float)) else (str(pts) if pts is not None else "N/A")

    link = str(raw.get("html_url") or raw.get("url") or raw.get("link") or "").strip()

    return NormalizedAssignment(
        id=raw_id,
        course=course,
        name=name,
        start_date=start_date_obj.isoformat(),
        due_date=due_date_obj.isoformat(),
        status=status,
        points=points_str,
        link=link,
    )
