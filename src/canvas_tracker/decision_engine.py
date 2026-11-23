from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from canvas_tracker.reconciler import UnifiedAssignment

logger = logging.getLogger(__name__)


# ==============================================================================
# Decision Engine Configuration & Protocol Interface
# ==============================================================================


class DecisionThresholds(BaseModel):
    critical_risk_score: float = 0.85
    high_risk_score: float = 0.65
    medium_risk_score: float = 0.40


class DecisionWeights(BaseModel):
    aspen_only_missing_bonus: float = 0.50
    due_within_24h_bonus: float = 0.30
    due_within_72h_bonus: float = 0.15
    high_grade_weight_bonus: float = 0.15


class DecisionModelConfig(BaseModel):
    enabled: bool = True
    provider: Literal["rule_based", "ml_scorer"] = "rule_based"
    thresholds: DecisionThresholds = Field(default_factory=DecisionThresholds)
    weights: DecisionWeights = Field(default_factory=DecisionWeights)


class DecisionModelProtocol(Protocol):
    """Pluggable contract allowing rule-based or future ML scorers to evaluate batches."""

    def evaluate_batch(
        self,
        assignments: list[UnifiedAssignment],
        student_context: dict[str, Any] | None = None,
    ) -> list[UnifiedAssignment]:
        ...


# ==============================================================================
# Deterministic Rule-Based Baseline Scorer
# ==============================================================================


class RuleBasedDecisionModel:
    """Deterministic risk and urgency scorer for student assignments."""

    def __init__(self, config: DecisionModelConfig | None = None) -> None:
        self.config = config or DecisionModelConfig()

    def _tier_and_action(
        self, score: float, is_discrepancy: bool, is_completed: bool
    ) -> tuple[Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"], str]:
        if is_completed:
            return "LOW", "COMPLETED"
        if is_discrepancy or score >= self.config.thresholds.critical_risk_score:
            return (
                "CRITICAL",
                "ESCALATE_TEACHER_SYNC" if is_discrepancy else "COMPLETE_IMMEDIATELY",
            )
        if score >= self.config.thresholds.high_risk_score:
            return "HIGH", "PRIORITIZE_TONIGHT"
        if score >= self.config.thresholds.medium_risk_score:
            return "MEDIUM", "SCHEDULE_THIS_WEEK"
        return "LOW", "MONITOR"

    def evaluate_batch(
        self,
        assignments: list[UnifiedAssignment],
        student_context: dict[str, Any] | None = None,
    ) -> list[UnifiedAssignment]:
        if not self.config.enabled:
            return assignments

        now_str = (student_context or {}).get("reference_date") or datetime.now().strftime(
            "%Y-%m-%d"
        )
        now_dt = datetime.strptime(now_str[:10], "%Y-%m-%d")
        w = self.config.weights

        enriched: list[UnifiedAssignment] = []
        for item in assignments:
            is_completed = item.status in {"Submitted", "Graded"}
            if is_completed:
                enriched.append(
                    item.model_copy(
                        update={
                            "risk_score": 0.0,
                            "urgency_tier": "LOW",
                            "recommended_action": "COMPLETED",
                            "rationale": "Assignment already submitted or graded.",
                        }
                    )
                )
                continue

            score = 0.20  # Baseline active assignment score
            reasons: list[str] = []

            # 1. Aspen-only discrepancy anomaly (0 or Missing in Aspen, absent from Canvas)
            if item.is_discrepancy:
                score += w.aspen_only_missing_bonus
                reasons.append(
                    f"Aspen reports '{item.aspen_score}' with no matching Canvas entry"
                )
            elif item.status in {"Missing", "Late"}:
                score += 0.35
                reasons.append(f"Marked '{item.status}' in LMS")

            # 2. Due-date proximity
            try:
                due_dt = datetime.strptime(item.due_date[:10], "%Y-%m-%d")
                days_left = (due_dt - now_dt).days
                if days_left < 0:
                    score += w.due_within_24h_bonus
                    reasons.append(f"Overdue by {abs(days_left)} day(s)")
                elif days_left <= 1:
                    score += w.due_within_24h_bonus
                    reasons.append("Due within 24 hours")
                elif days_left <= 3:
                    score += w.due_within_72h_bonus
                    reasons.append(f"Due in {days_left} days")
            except ValueError:
                pass

            # 3. Grade category weight or high point value
            if (item.grade_weight_pct and item.grade_weight_pct >= 15.0) or (
                item.points_possible and item.points_possible >= 50.0
            ):
                score += w.high_grade_weight_bonus
                reasons.append("High grade weight / point value")

            clamped_score = round(min(1.0, max(0.0, score)), 3)
            tier, action = self._tier_and_action(
                clamped_score, item.is_discrepancy, is_completed=False
            )

            enriched.append(
                item.model_copy(
                    update={
                        "risk_score": clamped_score,
                        "urgency_tier": tier,
                        "recommended_action": action,
                        "rationale": "; ".join(reasons) if reasons else "Standard upcoming work",
                    }
                )
            )

        return enriched


def build_decision_model(config: DecisionModelConfig) -> DecisionModelProtocol:
    """Factory function to instantiate the configured decision scoring provider."""
    if config.provider == "ml_scorer":
        logger.warning(
            "ml_scorer provider selected without trained artifact; falling back to RuleBasedDecisionModel"
        )
    return RuleBasedDecisionModel(config=config)