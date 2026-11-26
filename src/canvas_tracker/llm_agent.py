from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

try:
    from openai import OpenAI
except ImportError:
    OpenAI = Any  # type: ignore

from pydantic import BaseModel, Field

from canvas_tracker.reconciler import GrayZoneCandidate, UnifiedAssignment

logger = logging.getLogger(__name__)


# ==============================================================================
# LLM Configuration & Pydantic Structured Output Schemas
# ==============================================================================


class LLMAgentConfig(BaseModel):
    """Configuration for local Ollama / Open WebUI endpoint on bigdata."""

    enabled: bool = False
    base_url: str = "http://bigdata:11434/v1"
    api_key: str = "ollama"
    model: str = "gpt-oss:20b"
    timeout_seconds: float = 45.0
    temperature: float = 0.1
    resolve_gray_zone_matches: bool = True
    generate_daily_briefing: bool = True
    draft_teacher_inquiries: bool = True
    max_items_per_briefing: int = 10


class GrayZoneResolution(BaseModel):
    candidate_id: str
    is_same_assignment: bool
    reasoning: str


class GrayZoneBatchResponse(BaseModel):
    resolutions: list[GrayZoneResolution] = Field(default_factory=list)


class DiscrepancyTriageItem(BaseModel):
    uid: str
    probable_cause: str
    inquiry_draft: str = Field(
        description="Polite 2-sentence email note from parent/student asking the teacher about the missing/0 Aspen grade."
    )


class DiscrepancyTriageResponse(BaseModel):
    triaged_items: list[DiscrepancyTriageItem] = Field(default_factory=list)


class StudentDailyBriefing(BaseModel):
    headline: str
    bullet_priorities: list[str] = Field(default_factory=list)
    aspen_alert_summary: str | None = None

    def to_markdown(self) -> str:
        lines = [f"**{self.headline}**", ""]
        for bullet in self.bullet_priorities:
            lines.append(f"- {bullet}")
        if self.aspen_alert_summary:
            lines.extend(["", f"> **Aspen Check:** {self.aspen_alert_summary}"])
        return "\n".join(lines)


# ==============================================================================
# Agentic LLM Client (OpenAI-Compatible Ollama on bigdata)
# ==============================================================================


class AgenticLLMHook:
    """Provides structured LLM disambiguation, daily briefings, and inquiry drafts."""

    def __init__(self, config: LLMAgentConfig) -> None:
        self.config = config
        self._client: OpenAI | None = None
        if config.enabled:
            self._client = OpenAI(
                base_url=config.base_url,
                api_key=config.api_key,
                timeout=config.timeout_seconds,
            )

    def resolve_gray_zone_matches(
        self, candidates: list[GrayZoneCandidate]
    ) -> set[str]:
        """Ask the LLM whether 60-79% fuzzy title pairs refer to the same assignment."""
        if (
            not self.config.enabled
            or not self.config.resolve_gray_zone_matches
            or not candidates
            or self._client is None
        ):
            return set()

        prompt_payload = [c.model_dump() for c in candidates]
        try:
            completion = self._client.beta.chat.completions.parse(
                model=self.config.model,
                temperature=self.config.temperature,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You reconcile school assignments between Canvas LMS and Follett Aspen SIS. "
                            "Teachers often abbreviate titles in Aspen. Decide whether each pair refers "
                            "to the exact same coursework assignment."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(prompt_payload, ensure_ascii=False),
                    },
                ],
                response_format=GrayZoneBatchResponse,
            )
            parsed = completion.choices[0].message.parsed
            if not parsed:
                return set()
            confirmed = {
                r.candidate_id for r in parsed.resolutions if r.is_same_assignment
            }
            logger.info(
                "LLM confirmed %d of %d gray-zone Canvas/Aspen matches",
                len(confirmed),
                len(candidates),
            )
            return confirmed
        except Exception as exc:
            logger.warning("LLM gray-zone disambiguation failed (graceful fallback): %s", exc)
            return set()

    def triage_aspen_discrepancies(
        self,
        student_name: str,
        assignments: list[UnifiedAssignment],
    ) -> list[UnifiedAssignment]:
        """Draft concise parent/student teacher inquiry notes for Aspen-only 0/Missing items."""
        if (
            not self.config.enabled
            or not self.config.draft_teacher_inquiries
            or self._client is None
        ):
            return assignments

        discrepancies = [a for a in assignments if a.is_discrepancy]
        if not discrepancies:
            return assignments

        payload = [
            {
                "uid": d.uid,
                "course": d.course,
                "title": d.name,
                "due_date": d.due_date,
                "aspen_score": d.aspen_score,
            }
            for d in discrepancies
        ]

        try:
            completion = self._client.beta.chat.completions.parse(
                model=self.config.model,
                temperature=self.config.temperature,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            f"You assist {student_name}'s family with academic tracking. "
                            "For each assignment marked 0 or Missing in Aspen SIS that never appeared "
                            "in Canvas LMS, infer the probable cause (e.g., offline in-class worksheet, "
                            "gradebook sync omission) and write a polite, concise 2-sentence inquiry draft."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                ],
                response_format=DiscrepancyTriageResponse,
            )
            parsed = completion.choices[0].message.parsed
            if not parsed:
                return assignments

            drafts_by_uid = {
                item.uid: item.inquiry_draft for item in parsed.triaged_items
            }
            return [
                a.model_copy(update={"inquiry_draft": drafts_by_uid.get(a.uid, a.inquiry_draft)})
                for a in assignments
            ]
        except Exception as exc:
            logger.warning("LLM discrepancy triage failed (graceful fallback): %s", exc)
            return assignments

    def generate_daily_briefing(
        self,
        student_name: str,
        assignments: list[UnifiedAssignment],
    ) -> str | None:
        """Generate a concise Markdown daily briefing for Home Assistant."""
        if (
            not self.config.enabled
            or not self.config.generate_daily_briefing
            or self._client is None
        ):
            return None

        top_items = sorted(
            [a for a in assignments if a.status not in {"Submitted", "Graded"}],
            key=lambda x: x.risk_score,
            reverse=True,
        )[: self.config.max_items_per_briefing]

        if not top_items:
            return f"**All caught up!** {student_name} has no pending or missing assignments."

        compact_items = [
            {
                "course": a.course,
                "name": a.name,
                "due_date": a.due_date,
                "status": a.status,
                "source": a.source,
                "urgency_tier": a.urgency_tier,
                "rationale": a.rationale,
            }
            for a in top_items
        ]

        try:
            completion = self._client.beta.chat.completions.parse(
                model=self.config.model,
                temperature=self.config.temperature,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            f"Create a crisp, actionable daily homework briefing for {student_name}. "
                            "Prioritize Aspen-only missing/zero discrepancies first, then CRITICAL/HIGH "
                            "items due soon. Keep bullet points under 18 words each."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(compact_items, ensure_ascii=False),
                    },
                ],
                response_format=StudentDailyBriefing,
            )
            parsed = completion.choices[0].message.parsed
            return parsed.to_markdown() if parsed else None
        except Exception as exc:
            logger.warning("LLM daily briefing generation failed (graceful fallback): %s", exc)
            return None


# ==============================================================================
# External Agent Tool Schemas & State Query Helpers (Open WebUI / Assist)
# ==============================================================================


def get_agent_tool_schemas() -> list[dict[str, Any]]:
    """Return OpenAI-compatible tool definitions for external agent orchestrators."""
    return [
        {
            "type": "function",
            "function": {
                "name": "get_aspen_discrepancies",
                "description": (
                    "Retrieve assignments marked 0 or Missing in Follett Aspen SIS that never "
                    "appeared in Canvas LMS for a given student."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "student_id": {
                            "type": "string",
                            "description": "Student identifier from students.yaml (e.g., 'student_1').",
                        }
                    },
                    "required": ["student_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_missing_assignments",
                "description": "List all active Missing, Late, or CRITICAL urgency assignments for a student.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "student_id": {"type": "string"},
                    },
                    "required": ["student_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_upcoming_gantt_window",
                "description": "Retrieve upcoming assignments due within the next N days.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "student_id": {"type": "string"},
                        "days": {"type": "integer", "default": 7},
                    },
                    "required": ["student_id"],
                },
            },
        },
    ]


def execute_agent_tool(
    tool_name: str,
    arguments: dict[str, Any],
    state_dir: Path = Path("/app/data"),
) -> str:
    """Execute a read-only agent tool call against persisted state_<student>.json snapshots."""
    student_id = arguments.get("student_id", "student_1")
    state_path = state_dir / f"state_{student_id}.json"
    if not state_path.exists():
        return json.dumps({"error": f"No persisted state found for '{student_id}'"})

    records = json.loads(state_path.read_text(encoding="utf-8"))

    if tool_name == "get_aspen_discrepancies":
        matches = [r for r in records if r.get("is_discrepancy")]
        return json.dumps(matches, ensure_ascii=False)

    if tool_name == "get_missing_assignments":
        matches = [
            r
            for r in records
            if r.get("status") in {"Missing", "Late", "Aspen Only (Missing/0)"}
            or r.get("urgency_tier") == "CRITICAL"
        ]
        return json.dumps(matches, ensure_ascii=False)

    if tool_name == "get_upcoming_gantt_window":
        days = int(arguments.get("days", 7))
        matches = [
            r
            for r in records
            if r.get("status") not in {"Submitted", "Graded"}
        ][: days * 3]
        return json.dumps(matches, ensure_ascii=False)

    return json.dumps({"error": f"Unknown tool '{tool_name}'"})