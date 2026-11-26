"""Canvas assignment scraper supporting Go CLI binary and Canvas REST API fallback."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import requests

from canvas_tracker.config import StudentConfig, StudentEntryConfig
from canvas_tracker.reconciler import CanvasAssignmentRecord

logger = logging.getLogger(__name__)


class CanvasScraperError(Exception):
    """Raised when scraper execution fails."""


def _normalize_iso_date(raw_ts: str | None) -> str | None:
    if not raw_ts:
        return None
    cleaned = raw_ts.strip()
    if len(cleaned) >= 10:
        try:
            datetime.strptime(cleaned[:10], "%Y-%m-%d")
            return cleaned[:10]
        except ValueError:
            return None
    return None


def run_canvas_scraper(
    student: StudentEntryConfig | StudentConfig,
    canvas_base_url: str = "",
    default_lead_days: int = 7,
    binary_path: str = "canvas-scraper",
    output_dir: Optional[Path] = None,
) -> List[CanvasAssignmentRecord]:
    """
    Execute Canvas assignment extraction.
    1. First tries jdansak/Canvas-Scraper Go CLI binary with --json output.
    2. Gracefully falls back to direct Canvas LMS REST API if binary is unavailable or fails.
    3. Normalizes dates and builds CanvasAssignmentRecord list.
    """
    if isinstance(student, StudentConfig):
        entry = student.to_student_entry()
        if not canvas_base_url:
            canvas_base_url = f"https://{student.canvas_domain}"
        binary_path = student.canvas_scraper_bin
    else:
        entry = student

    canvas_base_url = canvas_base_url or os.getenv("CANVAS_BASE_URL", "https://knoxschools.instructure.com")

    # Check token/cookie
    token = entry.canvas.token
    cookie = entry.canvas.cookie
    auth_type = entry.canvas.auth_type

    raw_items: List[Dict[str, Any]] = []
    cli_available = bool(os.environ.get("USE_MOCK_SCRAPER") != "1")

    # 1. Attempt Go CLI invocation
    if cli_available and subprocess.run(["which", binary_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        env = os.environ.copy()
        env["CANVAS_URL"] = canvas_base_url
        if auth_type == "cookie" and cookie:
            env["CANVAS_COOKIE"] = cookie
        elif token:
            env["CANVAS_TOKEN"] = token

        cmd = [binary_path, "--json"]
        logger.info("Executing Canvas-Scraper CLI for student '%s'", entry.id)
        try:
            proc = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if proc.returncode == 0:
                raw_items = json.loads(proc.stdout or "[]")
                logger.info("Retrieved %d raw items from CLI for '%s'", len(raw_items), entry.id)
            else:
                logger.warning(
                    "Canvas-Scraper exited with code %d: %s. Falling back to REST API.",
                    proc.returncode,
                    proc.stderr.strip(),
                )
        except Exception as exc:
            logger.warning("Canvas-Scraper execution failed (%s). Falling back to REST API.", exc)

    # 2. Fallback to direct Canvas REST API if raw_items empty
    if not raw_items:
        logger.info("Fetching assignments for '%s' directly via Canvas REST API...", entry.id)
        raw_items = fetch_canvas_api_raw(entry, canvas_base_url)

    # 3. Normalize into CanvasAssignmentRecord items
    today_dt = datetime.now()
    today_str = today_dt.strftime("%Y-%m-%d")
    records: List[CanvasAssignmentRecord] = []

    for idx, item in enumerate(raw_items):
        due_str = _normalize_iso_date(item.get("due_at") or item.get("due_date"))
        if not due_str:
            if str(item.get("status", "")).lower() != "missing":
                continue
            due_str = today_str

        due_dt = datetime.strptime(due_str, "%Y-%m-%d")
        unlock_str = _normalize_iso_date(item.get("unlock_at") or item.get("created_at"))
        if unlock_str and unlock_str <= due_str:
            start_str = unlock_str
        else:
            fallback_start = due_dt - timedelta(days=default_lead_days)
            start_str = min(fallback_start, today_dt).strftime("%Y-%m-%d")

        raw_status = str(item.get("status", "Upcoming")).strip().title()
        if raw_status not in {"Missing", "Late", "Submitted", "Graded", "Upcoming"}:
            raw_status = "Upcoming"

        records.append(
            CanvasAssignmentRecord(
                canvas_id=item.get("id", f"c_{idx}"),
                course_id=item.get("course_id"),
                course_name=item.get("course_name") or item.get("course") or "Unknown Course",
                title=item.get("name") or item.get("title") or "Untitled Assignment",
                start_date=start_str,
                due_date=due_str,
                status=raw_status,
                points_possible=item.get("points_possible"),
                grade_weight_pct=item.get("weight") or item.get("grade_weight_pct"),
                link=item.get("html_url") or item.get("url") or "",
            )
        )

    logger.info("Normalized %d Canvas assignments for '%s'", len(records), entry.id)
    return records


def fetch_canvas_api_raw(entry: StudentEntryConfig, canvas_base_url: str) -> List[Dict[str, Any]]:
    """Fetch active courses and upcoming assignments directly via Canvas REST API."""
    token = entry.canvas.token
    if not token or token.startswith("YOUR_CANVAS_API_TOKEN"):
        logger.warning("No valid Canvas token provided for '%s'. Returning sample assignments.", entry.id)
        return get_sample_assignments(entry.display_name)

    base = canvas_base_url.rstrip("/")
    if not base.endswith("/api/v1"):
        api_url = f"{base}/api/v1"
    else:
        api_url = base

    headers = {"Authorization": f"Bearer {token}"}

    try:
        courses_resp = requests.get(
            f"{api_url}/courses",
            headers=headers,
            params={"enrollment_state": "active", "per_page": 50},
            timeout=30,
        )
        courses_resp.raise_for_status()
        courses = courses_resp.json()

        all_assignments: List[Dict[str, Any]] = []
        for course in courses:
            course_id = course.get("id")
            course_name = course.get("name", "General Course")

            assign_resp = requests.get(
                f"{api_url}/courses/{course_id}/assignments",
                headers=headers,
                params={"per_page": 50, "include[]": ["submission"]},
                timeout=30,
            )
            if assign_resp.status_code == 200:
                for a in assign_resp.json():
                    a["course"] = course_name
                    all_assignments.append(a)

        return all_assignments

    except Exception as e:
        logger.error("Error fetching from Canvas API for '%s': %s", entry.id, e)
        return []


def get_sample_assignments(student_name: str) -> List[Dict[str, Any]]:
    """Return fixture assignments for demonstration or when no token is present."""
    now = datetime.now(timezone.utc)
    return [
        {
            "id": 101,
            "name": f"Chapter 4 Problem Set - {student_name}",
            "course": "AP Calculus BC",
            "due_at": (now + timedelta(days=3)).strftime("%Y-%m-%dT23:59:59Z"),
            "unlock_at": (now - timedelta(days=2)).strftime("%Y-%m-%dT08:00:00Z"),
            "points_possible": 50,
            "html_url": "https://canvas.instructure.com/courses/1/assignments/101",
            "submission": {"workflow_state": "unsubmitted", "missing": False},
        },
        {
            "id": 102,
            "name": f"Lab Report 2: Optics - {student_name}",
            "course": "Physics",
            "due_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT23:59:59Z"),
            "unlock_at": None,
            "points_possible": 100,
            "html_url": "https://canvas.instructure.com/courses/2/assignments/102",
            "submission": {"workflow_state": "unsubmitted", "missing": True},
        },
        {
            "id": 103,
            "name": f"Essay: Industrial Revolution - {student_name}",
            "course": "World History",
            "due_at": (now + timedelta(days=8)).strftime("%Y-%m-%dT23:59:59Z"),
            "unlock_at": None,
            "points_possible": 100,
            "html_url": "https://canvas.instructure.com/courses/3/assignments/103",
            "submission": {"workflow_state": "unsubmitted", "missing": False},
        },
    ]


# Legacy v1 compatibility alias
def run_scraper_cli(student: StudentConfig, output_dir: Path) -> List[Dict[str, Any]]:
    """Legacy v1 helper returning raw dicts."""
    entry = student.to_student_entry()
    canvas_base_url = f"https://{student.canvas_domain}"
    records = run_canvas_scraper(entry, canvas_base_url=canvas_base_url, default_lead_days=7)
    return [
        {
            "id": r.canvas_id,
            "course": r.course_name,
            "name": r.title,
            "due_at": f"{r.due_date}T23:59:59Z",
            "unlock_at": f"{r.start_date}T00:00:00Z",
            "status": r.status,
            "points_possible": r.points_possible,
            "html_url": r.link,
        }
        for r in records
    ]
