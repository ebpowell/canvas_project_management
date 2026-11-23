from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from canvas_tracker.aspen_scraper import AspenAssignmentRecord

logger = logging.getLogger(__name__)


# ==============================================================================
# Pydantic Models for Unified & Reconciled Assignments
# ==============================================================================


class CanvasAssignmentRecord(BaseModel):
    """Normalized assignment record produced by normalizer.py from Canvas-Scraper."""

    canvas_id: str | int
    course_id: int | None = None
    course_name: str
    title: str
    start_date: str  # YYYY-MM-DD
    due_date: str  # YYYY-MM-DD
    status: str  # "Upcoming" | "Missing" | "Late" | "Submitted" | "Graded"
    points_possible: float | None = None
    grade_weight_pct: float | None = None
    link: str = ""


class ReconciliationConfig(BaseModel):
    """Thresholds loaded from global_defaults.reconciliation in students.yaml."""

    fuzzy_match_threshold: int = 80
    fuzzy_gray_zone_min: int = 60
    due_date_window_days: int = 3
    low_grade_alert_threshold: float = 60.0
    default_lead_days: int = 7


class GrayZoneCandidate(BaseModel):
    """Ambiguous Canvas-vs.-Aspen pair (60-79% similarity) queued for LLM review."""

    candidate_id: str
    course: str
    canvas_title: str
    canvas_due_date: str
    aspen_title: str
    aspen_due_date: str | None
    aspen_score: str
    fuzzy_score: float


class UnifiedAssignment(BaseModel):
    """Canonical record consumed by decision hooks, Google Sheets, and Home Assistant."""

    uid: str
    course: str
    name: str
    start_date: str  # YYYY-MM-DD
    due_date: str  # YYYY-MM-DD
    status: str
    source: Literal["canvas", "both", "aspen_only"]
    link: str = ""
    points_possible: float | None = None
    grade_weight_pct: float | None = None
    aspen_score: str | None = None
    aspen_status: str | None = None
    match_confidence: float | None = None
    is_discrepancy: bool = False
    inquiry_draft: str | None = None

    # Enriched by DecisionModelHook
    risk_score: float = 0.0
    urgency_tier: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"] = "MEDIUM"
    recommended_action: str = "MONITOR"
    rationale: str = ""


class ReconciliationResult(BaseModel):
    """Container returned by AssignmentReconciler.reconcile()."""

    unified_assignments: list[UnifiedAssignment] = Field(default_factory=list)
    gray_zone_candidates: list[GrayZoneCandidate] = Field(default_factory=list)
    unmatched_canvas_map: dict[str, CanvasAssignmentRecord] = Field(default_factory=dict)
    unmatched_aspen_map: dict[str, AspenAssignmentRecord] = Field(default_factory=dict)


# ==============================================================================
# Fuzzy Matching & Reconciliation Engine
# ==============================================================================


def _parse_ymd(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _within_date_window(
    canvas_due: str | None, aspen_due: str | None, window_days: int
) -> bool:
    """Return True if dates are within +/- window_days or if Aspen omitted a due date."""
    c_dt = _parse_ymd(canvas_due)
    a_dt = _parse_ymd(aspen_due)
    if c_dt is None or a_dt is None:
        return True
    return abs((c_dt - a_dt).days) <= window_days


def _same_course(c_rec: CanvasAssignmentRecord, a_rec: AspenAssignmentRecord) -> bool:
    """Match by explicit canvas_course_id mapping first, then canonical course name."""
    if a_rec.canvas_course_id is not None and c_rec.course_id is not None:
        return a_rec.canvas_course_id == c_rec.course_id
    return (
        fuzz.token_sort_ratio(
            c_rec.course_name.lower(), a_rec.canonical_course_name.lower()
        )
        >= 85
    )


class AssignmentReconciler:
    """Reconciles Canvas assignments against Follett Aspen SIS gradebook entries."""

    def __init__(self, config: ReconciliationConfig | None = None) -> None:
        self.config = config or ReconciliationConfig()

    def _build_aspen_only_item(
        self, idx: int, a_rec: AspenAssignmentRecord, today_str: str
    ) -> UnifiedAssignment:
        due = a_rec.due_date or a_rec.assigned_date or today_str
        due_dt = _parse_ymd(due) or datetime.strptime(today_str, "%Y-%m-%d")
        start = a_rec.assigned_date or (
            due_dt - timedelta(days=self.config.default_lead_days)
        ).strftime("%Y-%m-%d")

        is_discrepancy = a_rec.is_missing_or_zero
        status = (
            "Aspen Only (Missing/0)"
            if is_discrepancy
            else f"Aspen ({a_rec.status_flag})"
        )

        return UnifiedAssignment(
            uid=f"aspen_{a_rec.aspen_course_code}_{idx}",
            course=a_rec.canonical_course_name,
            name=a_rec.title,
            start_date=start,
            due_date=due,
            status=status,
            source="aspen_only",
            aspen_score=a_rec.score_raw or a_rec.status_flag,
            aspen_status=a_rec.status_flag,
            is_discrepancy=is_discrepancy,
        )

    def reconcile(
        self,
        canvas_records: list[CanvasAssignmentRecord],
        aspen_records: list[AspenAssignmentRecord],
        reference_date: str | None = None,
    ) -> ReconciliationResult:
        """Compare Canvas and Aspen records, returning unified items + gray-zone pairs."""
        today_str = reference_date or datetime.now().strftime("%Y-%m-%d")

        matched_canvas_ids: set[str] = set()
        matched_aspen_indices: set[int] = set()
        unified: list[UnifiedAssignment] = []
        gray_zone: list[GrayZoneCandidate] = []

        # Pass 1: Score every Aspen record against eligible Canvas records in the same course
        for a_idx, a_rec in enumerate(aspen_records):
            best_c_rec: CanvasAssignmentRecord | None = None
            best_score: float = -1.0

            for c_rec in canvas_records:
                c_key = str(c_rec.canvas_id)
                if c_key in matched_canvas_ids:
                    continue
                if not _same_course(c_rec, a_rec):
                    continue
                if not _within_date_window(
                    c_rec.due_date, a_rec.due_date, self.config.due_date_window_days
                ):
                    continue

                score = float(
                    fuzz.token_sort_ratio(c_rec.title.lower(), a_rec.title.lower())
                )
                if score > best_score:
                    best_score = score
                    best_c_rec = c_rec

            if best_c_rec is not None and best_score >= self.config.fuzzy_match_threshold:
                c_key = str(best_c_rec.canvas_id)
                matched_canvas_ids.add(c_key)
                matched_aspen_indices.add(a_idx)

                # Promote status if Aspen already marked the synced item Missing/Zero
                merged_status = best_c_rec.status
                if a_rec.is_missing_or_zero and best_c_rec.status not in {
                    "Missing",
                    "Late",
                }:
                    merged_status = "Missing"

                unified.append(
                    UnifiedAssignment(
                        uid=f"both_{c_key}",
                        course=a_rec.canonical_course_name or best_c_rec.course_name,
                        name=best_c_rec.title,
                        start_date=best_c_rec.start_date,
                        due_date=best_c_rec.due_date,
                        status=merged_status,
                        source="both",
                        link=best_c_rec.link,
                        points_possible=best_c_rec.points_possible,
                        grade_weight_pct=best_c_rec.grade_weight_pct,
                        aspen_score=a_rec.score_raw or a_rec.status_flag,
                        aspen_status=a_rec.status_flag,
                        match_confidence=round(best_score, 1),
                        is_discrepancy=False,
                    )
                )
            elif (
                best_c_rec is not None
                and best_score >= self.config.fuzzy_gray_zone_min
            ):
                gray_zone.append(
                    GrayZoneCandidate(
                        candidate_id=f"gz_{best_c_rec.canvas_id}_{a_idx}",
                        course=a_rec.canonical_course_name,
                        canvas_title=best_c_rec.title,
                        canvas_due_date=best_c_rec.due_date,
                        aspen_title=a_rec.title,
                        aspen_due_date=a_rec.due_date,
                        aspen_score=a_rec.score_raw or a_rec.status_flag,
                        fuzzy_score=round(best_score, 1),
                    )
                )

        # Pass 2: Emit remaining Canvas-only assignments
        unmatched_canvas: dict[str, CanvasAssignmentRecord] = {}
        for c_rec in canvas_records:
            c_key = str(c_rec.canvas_id)
            if c_key in matched_canvas_ids:
                continue
            unmatched_canvas[c_key] = c_rec
            unified.append(
                UnifiedAssignment(
                    uid=f"canvas_{c_key}",
                    course=c_rec.course_name,
                    name=c_rec.title,
                    start_date=c_rec.start_date,
                    due_date=c_rec.due_date,
                    status=c_rec.status,
                    source="canvas",
                    link=c_rec.link,
                    points_possible=c_rec.points_possible,
                    grade_weight_pct=c_rec.grade_weight_pct,
                )
            )

        # Pass 3: Emit remaining Aspen-only assignments (flagging Missing/0 anomalies)
        unmatched_aspen: dict[str, AspenAssignmentRecord] = {}
        for a_idx, a_rec in enumerate(aspen_records):
            if a_idx in matched_aspen_indices:
                continue
            unmatched_aspen[str(a_idx)] = a_rec
            unified.append(self._build_aspen_only_item(a_idx, a_rec, today_str))

        logger.info(
            "Reconciliation complete: total=%d, both=%d, canvas_only=%d, aspen_only=%d, gray_zone=%d",
            len(unified),
            len(matched_canvas_ids),
            len(unmatched_canvas),
            len(unmatched_aspen),
            len(gray_zone),
        )

        return ReconciliationResult(
            unified_assignments=unified,
            gray_zone_candidates=gray_zone,
            unmatched_canvas_map=unmatched_canvas,
            unmatched_aspen_map=unmatched_aspen,
        )

    def apply_llm_match_resolutions(
        self,
        result: ReconciliationResult,
        confirmed_candidate_ids: set[str],
    ) -> list[UnifiedAssignment]:
        """Merge gray-zone pairs confirmed by the LLM hook into 'both' records."""
        if not confirmed_candidate_ids:
            return result.unified_assignments

        updated = list(result.unified_assignments)
        for cand in result.gray_zone_candidates:
            if cand.candidate_id not in confirmed_candidate_ids:
                continue

            _, c_id, a_idx_str = cand.candidate_id.split("_", 2)
            # Remove standalone canvas_ and aspen_ entries and replace with merged 'both'
            updated = [
                u
                for u in updated
                if u.uid != f"canvas_{c_id}"
                and not u.uid.endswith(f"_{a_idx_str}")
            ]
            c_rec = result.unmatched_canvas_map.get(c_id)
            a_rec = result.unmatched_aspen_map.get(a_idx_str)
            if c_rec and a_rec:
                updated.append(
                    UnifiedAssignment(
                        uid=f"both_{c_id}",
                        course=cand.course,
                        name=c_rec.title,
                        start_date=c_rec.start_date,
                        due_date=c_rec.due_date,
                        status="Missing" if a_rec.is_missing_or_zero else c_rec.status,
                        source="both",
                        link=c_rec.link,
                        points_possible=c_rec.points_possible,
                        grade_weight_pct=c_rec.grade_weight_pct,
                        aspen_score=a_rec.score_raw or a_rec.status_flag,
                        aspen_status=a_rec.status_flag,
                        match_confidence=cand.fuzzy_score,
                        is_discrepancy=False,
                    )
                )

        return updated