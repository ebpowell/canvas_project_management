# Role & Objective
Act as a Principal Data & AI Systems Engineer. Extend the existing `canvas-tracker` repository (managed with `uv`, deployed via Docker Compose on `bigdata`, syncing to Google Sheets and Home Assistant via MQTT) to add:
1. **Follett Aspen SIS Gradebook Extraction** via headless Playwright.
2. **Canvas-vs.-Aspen Reconciliation** to detect offline/unposted assignments marked `0` or `Missing` in Aspen that never appeared in Canvas.
3. **Pluggable Decision-Model & Agentic LLM Hooks** designed for future integration with a local LLM (e.g., Ollama / Open WebUI on `bigdata`) and a decision/prioritization model.

# Existing Context & Assumptions
- The repository already contains `pyproject.toml` (`uv`), `src/canvas_tracker/` (`scraper.py`, `normalizer.py`, `sheets_client.py`, `mqtt_publisher.py`, `main.py`), `config/students.yaml`, `Dockerfile`, and `docker-compose.yml`.
- Maintain strict adherence to `uv` packaging, Pydantic v2 validation, retained Home Assistant MQTT Discovery payloads on host `bigdata`, and non-destructive Google Sheets tab updates.

# Architectural Additions & Required Modules

## 1. Playwright Aspen SIS Scraper (`src/canvas_tracker/aspen_scraper.py`)
- Add `playwright` and `beautifulsoup4` via `uv` and update the `Dockerfile` to install Chromium system dependencies cleanly.
- Implement `AspenScraper` to authenticate into the Follett Aspen Parent/Student portal (`ASPEN_BASE_URL`, credentials configured per student in `students.yaml` / `.env`).
- Navigate to **Academics -> [Class] -> Assignments** for each active course, extracting:
  - `aspen_course_code`, `aspen_course_name`, `title`, `category`, `assigned_date`, `due_date`, `score_raw`, `score_pct`, and `status_flag` (`Missing`, `M`, `0`, `Graded`, `Ungraded`).
- Include resilient selector fallbacks, configurable request throttling, and optional HTML snapshot dumping (`./data/debug/aspen/`) when `DEBUG_SCRAPER=true`.

## 2. Canvas-vs.-Aspen Reconciler (`src/canvas_tracker/reconciler.py`)
- Add `rapidfuzz` to `pyproject.toml`.
- Implement `AssignmentReconciler` using course mappings defined in `students.yaml` (mapping Aspen course codes to Canvas course IDs/names).
- Match Aspen entries to normalized Canvas assignments using `rapidfuzz.fuzz.token_sort_ratio >= 80` within a configurable `±3 day` due-date window.
- Emit unified `UnifiedAssignment` Pydantic models tagging each item with `source`: `"canvas"`, `"both"`, or `"aspen_only"`.
- Specifically flag **Discrepancy Anomalies**: any `"aspen_only"` assignment where `status_flag` is `Missing`/`M` or `score_raw == 0` (or `score_pct < 60%`).

## 3. Decision-Model & Agentic LLM Hooks (`src/canvas_tracker/hooks/`)
Create a clean, decoupled `hooks/` subpackage with abstract interfaces (`Protocol` / ABC) and configuration toggles in `students.yaml` (`enable_decision_model: false`, `enable_llm_agent: false`) so the core pipeline runs deterministically even when AI hooks are disabled:

- **`hooks/decision_engine.py` (Decision & Prioritization Model Hook):**
  - Define a `DecisionModelHook` interface (`evaluate_batch(assignments: list[UnifiedAssignment], student_context: dict) -> list[PrioritizedAction]`) that enriches each assignment with:
    - `risk_score` (`0.0` to `1.0`), `urgency_tier` (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`), `recommended_action` (e.g., `"ESCALATE_TEACHER_SYNC"`, `"PRIORITIZE_TONIGHT"`, `"MONITOR"`), and `rationale`.
  - Provide a default deterministic rule-based baseline (`RuleBasedDecisionModel`) that scores `"aspen_only"` zero/missing items and high-weight upcoming exams highest, while allowing a future ML/heuristic scoring model to be swapped in via config.

- **`hooks/llm_agent.py` (Agentic LLM & Tool-Calling Hook):**
  - Implement an `AgenticLLMHook` client targeting any **OpenAI-compatible REST endpoint** (`LLM_BASE_URL`, defaulting to `http://bigdata:11434/v1` for Ollama or Open WebUI, `LLM_MODEL`, `LLM_API_KEY`) using Pydantic structured outputs.
  - Define structured Pydantic output schemas (`StudentDailyBriefing`, `DiscrepancyTriageResult`) so an LLM can:
    1. Resolve ambiguous Canvas-vs.-Aspen title matches where fuzzy string similarity falls in the gray zone (`60–79%`).
    2. Generate concise daily action summaries and draft parent/student inquiry notes for `"aspen_only"` missing/zero discrepancies.
  - Expose read-only **Agent Tool Definitions** (`tools.py` returning JSON schemas for `get_missing_assignments`, `get_aspen_discrepancies`, `get_upcoming_gantt_window`) so an external agentic orchestrator (e.g., Open WebUI, LangGraph, or Home Assistant Assist) can query the tracker state directly.

## 4. Pipeline, Google Sheets & Home Assistant Updates
- **Pipeline (`main.py`):** Update execution flow to `Canvas Scrape -> Aspen Scrape -> Reconcile -> Decision Hook -> (Optional) LLM Hook -> Sheets Sync -> MQTT Publish`. Persist normalized daily snapshots to `./data/state_<student>.json` so external agent tools can read the latest state on demand.
- **Google Sheets (`sheets_client.py`):**
  - Add `Source` (`Canvas`, `Both`, `Aspen-Only`) and `Risk Score` columns to `Raw_Data_<Student>`, marking `"aspen_only"` zero/missing rows with `Status = "Aspen Only (Missing/0)"` so they stand out on the Gantt chart.
  - Create/update a dedicated `Discrepancies_<Student>` tab listing only unresolved `"aspen_only"` anomalies and LLM triage notes.
- **Home Assistant (`mqtt_publisher.py` & `homeassistant/`):**
  - Extend the retained MQTT payload on `homeassistant/sensor/canvas_assignments_<student>/state` to include `aspen_discrepancy_count`, `aspen_discrepancies` array, `top_priority_actions`, and `daily_llm_briefing`.
  - Update `homeassistant/lovelace_dashboard.yaml` and `automations.yaml` to surface an **Aspen Discrepancy Alert Card** and trigger a high-priority mobile notification whenever `aspen_discrepancy_count > 0`.

# Verification Plan
1. Run `uv sync` and verify Playwright, `rapidfuzz`, and `openai` SDK dependencies resolve cleanly.
2. Add unit tests in `tests/test_reconciler.py` and `tests/test_hooks.py` covering:
   - Exact and fuzzy matching (`>= 80%`) vs. gray-zone (`60–79%`) vs. `"aspen_only"` zero/missing detection.
   - Deterministic `RuleBasedDecisionModel` scoring and mocked OpenAI-compatible `AgenticLLMHook` structured output parsing.
3. Run `uv run pytest` and validate `docker compose config --quiet`.