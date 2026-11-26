import pytest
from datetime import datetime, timezone
from canvas_tracker.aspen_scraper import AspenAssignmentRecord
from canvas_tracker.reconciler import (
    AssignmentReconciler,
    CanvasAssignmentRecord,
    ReconciliationConfig,
)
from canvas_tracker.decision_engine import (
    DecisionModelConfig,
    build_decision_model,
)
from canvas_tracker.mqtt_publisher import build_sensor_payload


def test_reconciliation_exact_and_fuzzy_match():
    reconciler = AssignmentReconciler(ReconciliationConfig(fuzzy_match_threshold=80))

    canvas_records = [
        CanvasAssignmentRecord(
            canvas_id=1,
            course_name="Algebra II",
            title="Unit 3 Quadratic Functions Quiz",
            start_date="2026-10-01",
            due_date="2026-10-08",
            status="Upcoming",
            points_possible=50.0,
        ),
        CanvasAssignmentRecord(
            canvas_id=2,
            course_name="Chemistry",
            title="Periodic Trends Lab",
            start_date="2026-10-02",
            due_date="2026-10-09",
            status="Submitted",
            points_possible=100.0,
        ),
    ]

    aspen_records = [
        AspenAssignmentRecord(
            aspen_course_code="0301-1",
            aspen_course_name="Algebra II",
            canonical_course_name="Algebra II",
            title="Unit 3 Quadratic Functions Quiz",
            due_date="2026-10-08",
            score_raw="45",
            score_numeric=45.0,
            status_flag="Graded",
        ),
        AspenAssignmentRecord(
            aspen_course_code="0402-1",
            aspen_course_name="Biology",
            canonical_course_name="Biology",
            title="Cell Structure Worksheet",
            due_date="2026-10-05",
            score_raw="0",
            score_numeric=0.0,
            status_flag="Missing",
            is_missing_or_zero=True,
        ),
    ]

    result = reconciler.reconcile(canvas_records=canvas_records, aspen_records=aspen_records)

    # 1 matched ("both"), 1 canvas_only ("Chemistry"), 1 aspen_only ("Biology")
    assert len(result.unified_assignments) == 3

    matched = next(u for u in result.unified_assignments if u.course == "Algebra II")
    assert matched.source == "both"
    assert matched.aspen_score == "45"

    canvas_only = next(u for u in result.unified_assignments if u.course == "Chemistry")
    assert canvas_only.source == "canvas"

    aspen_only = next(u for u in result.unified_assignments if u.course == "Biology")
    assert aspen_only.source == "aspen_only"
    assert aspen_only.is_discrepancy is True


def test_decision_engine_risk_scoring():
    reconciler = AssignmentReconciler()
    result = reconciler.reconcile(
        canvas_records=[],
        aspen_records=[
            AspenAssignmentRecord(
                aspen_course_code="0101",
                aspen_course_name="AP Calculus",
                canonical_course_name="AP Calculus",
                title="Chapter 1 Review",
                due_date="2026-10-10",
                score_raw="0",
                score_numeric=0.0,
                status_flag="Missing",
                is_discrepancy=True,
            )
        ],
    )

    model = build_decision_model(DecisionModelConfig())
    scored = model.evaluate_batch(result.unified_assignments)

    assert len(scored) == 1
    item = scored[0]
    # Aspen only missing should receive bonus
    assert item.risk_score >= 0.50
    assert item.recommended_action in {"ESCALATE_TEACHER_SYNC", "PRIORITIZE_TONIGHT", "SCHEDULE_THIS_WEEK", "MONITOR"}


def test_mqtt_payload_assembly():
    records = [
        {
            "course": "AP Calculus",
            "name": "Chapter 1 Review",
            "start_date": "2026-10-01",
            "due_date": "2026-10-10",
            "status": "Aspen Only (Missing/0)",
            "source": "aspen_only",
            "link": "",
            "risk_score": 0.85,
            "urgency_tier": "CRITICAL",
            "recommended_action": "ESCALATE_TEACHER_SYNC",
            "is_discrepancy": True,
            "aspen_score": "0",
            "inquiry_draft": "Please check on Chapter 1 Review.",
        }
    ]

    payload = build_sensor_payload(records, daily_llm_briefing="All caught up except Calculus.")

    assert payload.active_count == 1
    assert payload.missing_count == 1
    assert payload.aspen_discrepancy_count == 1
    assert len(payload.aspen_discrepancies) == 1
    assert payload.aspen_discrepancies[0].aspen_score == "0"
    assert payload.daily_llm_briefing == "All caught up except Calculus."
