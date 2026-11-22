"""Unit tests for assignment normalization and status classification."""

from datetime import date, timedelta
from canvas_tracker.normalizer import (
    calculate_start_date,
    determine_status,
    normalize_assignment,
)

FIXED_TODAY = date(2026, 11, 20)


def test_start_date_with_explicit_unlock():
    unlock = date(2026, 11, 10)
    due = date(2026, 11, 30)
    res = calculate_start_date(unlock, due, today=FIXED_TODAY)
    assert res == unlock


def test_start_date_missing_unlock_greater_than_7_days():
    due = date(2026, 11, 30)
    # due - 7 is 2026-11-23, which is >= FIXED_TODAY (2026-11-20)
    res = calculate_start_date(None, due, today=FIXED_TODAY)
    assert res == date(2026, 11, 23)


def test_start_date_missing_unlock_within_7_days():
    due = date(2026, 11, 24)
    # due - 7 is 2026-11-17, which is < FIXED_TODAY (2026-11-20), so falls back to FIXED_TODAY
    res = calculate_start_date(None, due, today=FIXED_TODAY)
    assert res == FIXED_TODAY


def test_status_graded():
    status = determine_status(
        raw_status=None,
        submission_data={"workflow_state": "graded", "grade": "95"},
        due_date=date(2026, 11, 15),
        today=FIXED_TODAY,
    )
    assert status == "Graded"


def test_status_submitted():
    status = determine_status(
        raw_status=None,
        submission_data={"workflow_state": "submitted", "submitted_at": "2026-11-18T10:00:00Z"},
        due_date=date(2026, 11, 22),
        today=FIXED_TODAY,
    )
    assert status == "Submitted"


def test_status_submitted_late():
    status = determine_status(
        raw_status=None,
        submission_data={"workflow_state": "submitted", "late": True},
        due_date=date(2026, 11, 15),
        today=FIXED_TODAY,
    )
    assert status == "Late"


def test_status_overdue_missing():
    status = determine_status(
        raw_status=None,
        submission_data={"workflow_state": "unsubmitted", "missing": False},
        due_date=date(2026, 11, 15),  # in past
        today=FIXED_TODAY,
    )
    assert status == "Missing"


def test_status_future_upcoming():
    status = determine_status(
        raw_status=None,
        submission_data={"workflow_state": "unsubmitted"},
        due_date=date(2026, 11, 28),  # in future
        today=FIXED_TODAY,
    )
    assert status == "Upcoming"


def test_normalize_assignment_payload_and_conversions():
    raw_payload = {
        "id": 401,
        "name": "Midterm Project",
        "course": "AP Computer Science",
        "due_at": "2026-11-27T23:59:59Z",
        "unlock_at": "2026-11-15T08:00:00Z",
        "points_possible": 100,
        "html_url": "https://canvas.example.com/assignments/401",
        "submission": {"workflow_state": "unsubmitted"},
    }

    item = normalize_assignment(raw_payload, today=FIXED_TODAY)
    assert item.id == "401"
    assert item.course == "AP Computer Science"
    assert item.name == "Midterm Project"
    assert item.start_date == "2026-11-15"
    assert item.due_date == "2026-11-27"
    assert item.status == "Upcoming"
    assert item.points == "100"

    # Test sheet row format
    row = item.to_sheet_row()
    assert row == [
        "AP Computer Science",
        "Midterm Project",
        "2026-11-15",
        "2026-11-27",
        "Upcoming",
        "100",
        "https://canvas.example.com/assignments/401",
    ]

    # Test MQTT dictionary format
    mqtt_dict = item.to_mqtt_item()
    assert mqtt_dict["id"] == "401"
    assert mqtt_dict["start"] == "2026-11-15"
    assert mqtt_dict["end"] == "2026-11-27"
    assert mqtt_dict["status"] == "Upcoming"


def test_normalize_assignment_null_dates():
    raw_payload = {
        "id": 999,
        "name": "Open Ended Exploration",
        "course": "Art",
        "due_at": None,
        "unlock_at": None,
        "points_possible": None,
        "html_url": "https://canvas.example.com/assignments/999",
    }
    item = normalize_assignment(raw_payload, today=FIXED_TODAY)
    assert item.start_date == FIXED_TODAY.isoformat()
    assert item.due_date == (FIXED_TODAY + timedelta(days=7)).isoformat()
    assert item.points == "N/A"
