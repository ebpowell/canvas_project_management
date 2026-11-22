"""Subprocess wrapper for Canvas-Scraper CLI with Canvas API fallback."""

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List
import requests

from canvas_tracker.config import StudentConfig

logger = logging.getLogger(__name__)


class CanvasScraperError(Exception):
    """Raised when scraper execution fails."""


def run_scraper_cli(student: StudentConfig, output_dir: Path) -> List[Dict[str, Any]]:
    """
    Invoke canvas-scraper CLI binary with --json output.
    Falls back to direct Canvas REST API if binary is not present.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / f"{student.name.lower()}_raw.json"

    # If canvas-scraper binary exists, invoke it via subprocess
    scraper_bin = student.canvas_scraper_bin
    cli_available = bool(os.environ.get("USE_MOCK_SCRAPER") != "1")

    if cli_available and subprocess.run(["which", scraper_bin], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        cmd = [
            scraper_bin,
            "--domain", student.canvas_domain,
            "--token", student.canvas_token,
            "--json",
            "--output", str(out_file),
        ]
        logger.info("Executing canvas-scraper binary for student %s...", student.name)
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
            )
            logger.debug("Scraper output: %s", res.stdout)
            if out_file.exists():
                with open(out_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except subprocess.CalledProcessError as e:
            logger.warning("canvas-scraper binary returned error: %s. Falling back to REST API.", e.stderr)
        except Exception as e:
            logger.warning("Subprocess execution failed (%s). Falling back to REST API.", e)

    # Fallback to direct Canvas LMS REST API
    logger.info("Fetching assignments for %s directly via Canvas REST API...", student.name)
    return fetch_canvas_api(student)


def fetch_canvas_api(student: StudentConfig) -> List[Dict[str, Any]]:
    """Fetch active courses and upcoming assignments directly via Canvas REST API."""
    if not student.canvas_token or student.canvas_token == "YOUR_CANVAS_API_TOKEN_HERE":
        logger.warning("No valid Canvas token provided for %s. Returning mock/sample data.", student.name)
        return get_sample_assignments(student.name)

    base_url = f"https://{student.canvas_domain}/api/v1"
    headers = {"Authorization": f"Bearer {student.canvas_token}"}

    try:
        courses_resp = requests.get(
            f"{base_url}/courses",
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

            if course_name in student.excluded_courses:
                continue

            assign_resp = requests.get(
                f"{base_url}/courses/{course_id}/assignments",
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
        logger.error("Error fetching from Canvas API for %s: %s", student.name, e)
        return []


def get_sample_assignments(student_name: str) -> List[Dict[str, Any]]:
    """Return fixture assignments for demonstration or when no token is present."""
    from datetime import datetime, timedelta, timezone

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
