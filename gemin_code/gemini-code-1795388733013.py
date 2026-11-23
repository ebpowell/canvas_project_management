from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from canvas_tracker.aspen_scraper import (
    AspenRuntimeConfig,
    AspenStudentConfig,
    scrape_aspen_sync,
)
from canvas_tracker.hooks import (
    AgenticLLMHook,
    DecisionModelConfig,
    LLMAgentConfig,
    build_decision_model,
)
from canvas_tracker.mqtt_publisher import (
    HomeAssistantMQTTPublisher,
    MQTTConfig,
    build_sensor_payload,
)
from canvas_tracker.reconciler import (
    AssignmentReconciler,
    CanvasAssignmentRecord,
    ReconciliationConfig,
    UnifiedAssignment,
)

# ==============================================================================
# Structured Logging Setup
# ==============================================================================


class JsonLogFormatter(logging.Formatter):
    """Emit structured JSON log lines compatible with Docker json-file logging."""

    def format(self, record: logging.LogRecord) -> str:
        log_obj: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj, ensure_ascii=False)


def configure_logging(level_name: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level_name.upper(), logging.INFO))


logger = logging.getLogger("canvas_tracker.main")


# ==============================================================================
# YAML + .env Configuration Loader
# ==============================================================================

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate_env(obj: Any) -> Any:
    """Recursively expand ${VAR_NAME} placeholders in loaded YAML structures."""
    if isinstance(obj, str):
        return _ENV_PATTERN.sub(lambda m: os.getenv(m.group(1), ""), obj)
    if isinstance(obj, list):
        return [_interpolate_env(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _interpolate_env(v) for k, v in obj.items()}
    return obj


class CanvasStudentConfig(BaseModel):
    auth_type: Literal["token", "cookie"] = "token"
    token: str = ""
    cookie: str = ""
    include_concluded_courses: bool = False


class GoogleSheetsOutputConfig(BaseModel):
    raw_data_tab: str
    discrepancies_tab: str


class HomeAssistantOutputConfig(BaseModel):
    sensor_object_id: str
    device_name: str
    notify_services: list[str] = Field(default_factory=list)


class StudentOutputsConfig(BaseModel):
    google_sheets: GoogleSheetsOutputConfig
    home_assistant: HomeAssistantOutputConfig


class StudentEntryConfig(BaseModel):
    id: str
    display_name: str
    enabled: bool = True
    canvas: CanvasStudentConfig
    aspen: AspenStudentConfig
    outputs: StudentOutputsConfig


class GlobalDefaultsConfig(BaseModel):
    default_lead_days: int = 7
    gantt_horizon_days: int = 21
    reconciliation: ReconciliationConfig = Field(default_factory=ReconciliationConfig)
    decision_model: DecisionModelConfig = Field(default_factory=DecisionModelConfig)
    llm_agent: LLMAgentConfig = Field(default_factory=LLMAgentConfig)


class AppConfig(BaseModel):
    global_defaults: GlobalDefaultsConfig = Field(default_factory=GlobalDefaultsConfig)
    students: list[StudentEntryConfig] = Field(default_factory=list)


def load_app_config(config_path: Path) -> AppConfig:
    """Load students.yaml, resolve .env placeholders, and overlay .env LLM overrides."""
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    raw_yaml = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    interpolated = _interpolate_env(raw_yaml)
    cfg = AppConfig.model_validate(interpolated)

    # Overlay .env connection parameters onto LLMAgentConfig
    cfg.global_defaults.llm_agent.base_url = os.getenv(
        "LLM_BASE_URL", cfg.global_defaults.llm_agent.base_url
    )
    cfg.global_defaults.llm_agent.api_key = os.getenv(
        "LLM_API_KEY", cfg.global_defaults.llm_agent.api_key
    )
    cfg.global_defaults.llm_agent.model = os.getenv(
        "LLM_MODEL", cfg.global_defaults.llm_agent.model
    )
    cfg.global_defaults.llm_agent.timeout_seconds = float(
        os.getenv("LLM_TIMEOUT_SECONDS", str(cfg.global_defaults.llm_agent.timeout_seconds))
    )
    cfg.global_defaults.llm_agent.temperature = float(
        os.getenv("LLM_TEMPERATURE", str(cfg.global_defaults.llm_agent.temperature))
    )
    cfg.global_defaults.reconciliation.default_lead_days = (
        cfg.global_defaults.default_lead_days
    )
    return cfg


# ==============================================================================
# Stage 1: Canvas-Scraper CLI Wrapper & Date Normalizer
# ==============================================================================


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
    student: StudentEntryConfig,
    canvas_base_url: str,
    default_lead_days: int,
    binary_path: str = "canvas-scraper",
) -> list[CanvasAssignmentRecord]:
    """Execute jdansak/Canvas-Scraper in --json mode and normalize Gantt dates."""
    env = os.environ.copy()
    env["CANVAS_URL"] = canvas_base_url
    if student.canvas.auth_type == "cookie" and student.canvas.cookie:
        env["CANVAS_COOKIE"] = student.canvas.cookie
    elif student.canvas.token:
        env["CANVAS_TOKEN"] = student.canvas.token
    else:
        raise ValueError(f"No Canvas token or cookie configured for '{student.id}'")

    cmd = [binary_path, "--json"]
    logger.info("Executing Canvas-Scraper CLI for student '%s'", student.id)

    proc = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Canvas-Scraper exited with code {proc.returncode}: {proc.stderr.strip()}"
        )

    raw_items = json.loads(proc.stdout or "[]")
    today_dt = datetime.now()
    today_str = today_dt.strftime("%Y-%m-%d")
    records: list[CanvasAssignmentRecord] = []

    for idx, item in enumerate(raw_items):
        due_str = _normalize_iso_date(item.get("due_at") or item.get("due_date"))
        if not due_str:
            # Skip undated placeholder syllabus items unless marked missing
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

    logger.info("Normalized %d Canvas assignments for '%s'", len(records), student.id)
    return records


# ==============================================================================
# Stage 5: Google Sheets Sync (Raw_Data_<Student> & Discrepancies_<Student>)
# ==============================================================================


def sync_to_google_sheets(
    student: StudentEntryConfig,
    assignments: list[UnifiedAssignment],
    spreadsheet_id: str,
    service_account_file: Path,
) -> None:
    """Update Raw_Data and Discrepancies tabs in place via gspread."""
    if not spreadsheet_id or not service_account_file.exists():
        logger.warning(
            "Google Sheets credentials or spreadsheet ID missing; skipping Sheets sync."
        )
        return

    import gspread

    gc = gspread.service_account(filename=str(service_account_file))
    sh = gc.open_by_key(spreadsheet_id)

    def _get_or_create_tab(title: str, rows: int = 200, cols: int = 10) -> gspread.Worksheet:
        try:
            return sh.worksheet(title)
        except gspread.WorksheetNotFound:
            return sh.add_worksheet(title=title, rows=rows, cols=cols)

    # 1. Update Raw_Data_<Student> tab in place (preserves Gantt Timeline references)
    raw_ws = _get_or_create_tab(student.outputs.google_sheets.raw_data_tab)
    raw_headers = [
        "Course",
        "Assignment",
        "Start Date",
        "Due Date",
        "Status",
        "Weight / Pts",
        "Source",
        "Risk Score",
        "Urgency Tier",
        "Link",
    ]
    sorted_items = sorted(assignments, key=lambda a: (a.due_date, -a.risk_score))
    raw_rows = [raw_headers]
    for a in sorted_items:
        weight_or_pts = (
            f"{a.grade_weight_pct}%"
            if a.grade_weight_pct is not None
            else (str(a.points_possible) if a.points_possible is not None else "")
        )
        raw_rows.append(
            [
                a.course,
                a.name,
                a.start_date,
                a.due_date,
                a.status,
                weight_or_pts,
                a.source,
                a.risk_score,
                a.urgency_tier,
                a.link,
            ]
        )

    raw_ws.clear()
    raw_ws.update(range_name="A1", values=raw_rows, value_input_option="USER_ENTERED")

    # 2. Update Discrepancies_<Student> tab
    disc_ws = _get_or_create_tab(student.outputs.google_sheets.discrepancies_tab)
    disc_headers = [
        "Course",
        "Assignment",
        "Aspen Due Date",
        "Aspen Score",
        "Risk Score",
        "Recommended Action",
        "LLM Teacher Inquiry Draft",
    ]
    discrepancies = [a for a in sorted_items if a.is_discrepancy]
    disc_rows = [disc_headers]
    for d in discrepancies:
        disc_rows.append(
            [
                d.course,
                d.name,
                d.due_date,
                d.aspen_score or "Missing",
                d.risk_score,
                d.recommended_action,
                d.inquiry_draft or "",
            ]
        )

    disc_ws.clear()
    disc_ws.update(range_name="A1", values=disc_rows, value_input_option="USER_ENTERED")

    logger.info(
        "Synced %d total rows and %d Aspen discrepancies to Google Sheets for '%s'",
        len(sorted_items),
        len(discrepancies),
        student.id,
    )


# ==============================================================================
# Per-Student Pipeline Orchestrator
# ==============================================================================


def filter_gantt_horizon(
    assignments: list[UnifiedAssignment],
    horizon_days: int,
    reference_date: str | None = None,
) -> list[UnifiedAssignment]:
    """Keep all Missing/Discrepancy items plus active work within the Gantt lookahead window."""
    now_dt = datetime.strptime(
        (reference_date or datetime.now().strftime("%Y-%m-%d"))[:10], "%Y-%m-%d"
    )
    cutoff_dt = now_dt + timedelta(days=horizon_days)
    past_grace_dt = now_dt - timedelta(days=7)

    filtered: list[UnifiedAssignment] = []
    for item in assignments:
        if item.is_discrepancy or item.status in {
            "Missing",
            "Late",
            "Aspen Only (Missing/0)",
        }:
            filtered.append(item)
            continue
        try:
            due_dt = datetime.strptime(item.due_date[:10], "%Y-%m-%d")
            if past_grace_dt <= due_dt <= cutoff_dt:
                filtered.append(item)
        except ValueError:
            filtered.append(item)
    return filtered


def process_student(
    student: StudentEntryConfig,
    defaults: GlobalDefaultsConfig,
    aspen_runtime: AspenRuntimeConfig,
    mqtt_publisher: HomeAssistantMQTTPublisher,
    state_dir: Path,
) -> None:
    """Run all 6 stages for a single configured student."""
    logger.info("=== Starting pipeline run for student '%s' ===", student.id)

    canvas_base_url = os.getenv("CANVAS_BASE_URL", "https://knoxschools.instructure.com")
    spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID", "")
    sa_file = Path(
        os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "/app/secrets/google-service-account.json")
    )

    # 1. Extract Canvas assignments
    canvas_records = run_canvas_scraper(
        student=student,
        canvas_base_url=canvas_base_url,
        default_lead_days=defaults.default_lead_days,
    )

    # 2. Extract Aspen SIS gradebook entries (fault-isolated)
    aspen_records = []
    if student.aspen.enabled:
        try:
            aspen_records = scrape_aspen_sync(
                runtime_config=aspen_runtime,
                student_cfg=student.aspen,
                low_grade_threshold=defaults.reconciliation.low_grade_alert_threshold,
            )
        except Exception as exc:
            logger.error(
                "Aspen scrape failed for '%s'; continuing with Canvas-only records: %s",
                student.id,
                exc,
            )

    # 3. Fuzzy Reconcile Canvas vs. Aspen
    reconciler = AssignmentReconciler(config=defaults.reconciliation)
    rec_result = reconciler.reconcile(
        canvas_records=canvas_records,
        aspen_records=aspen_records,
    )

    # 4. Optional LLM Gray-Zone Match Disambiguation
    llm_hook = AgenticLLMHook(config=defaults.llm_agent)
    if rec_result.gray_zone_candidates:
        confirmed_ids = llm_hook.resolve_gray_zone_matches(
            rec_result.gray_zone_candidates
        )
        unified = reconciler.apply_llm_match_resolutions(rec_result, confirmed_ids)
    else:
        unified = rec_result.unified_assignments

    # 5. Decision Engine Scoring & LLM Triage/Briefing
    decision_model = build_decision_model(defaults.decision_model)
    scored_assignments = decision_model.evaluate_batch(unified)
    scored_assignments = llm_hook.triage_aspen_discrepancies(
        student_name=student.display_name,
        assignments=scored_assignments,
    )
    daily_briefing = llm_hook.generate_daily_briefing(
        student_name=student.display_name,
        assignments=scored_assignments,
    )

    # 6a. Persist full state snapshot to disk for external agent tools
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / f"state_{student.id}.json"
    state_file.write_text(
        json.dumps(
            [a.model_dump() for a in scored_assignments],
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 6b. Sync full dataset to Google Sheets
    try:
        sync_to_google_sheets(
            student=student,
            assignments=scored_assignments,
            spreadsheet_id=spreadsheet_id,
            service_account_file=sa_file,
        )
    except Exception as exc:
        logger.error("Google Sheets sync failed for '%s': %s", student.id, exc)

    # 6c. Publish 21-day Gantt horizon + discrepancies to Home Assistant via MQTT
    ha_window = filter_gantt_horizon(
        scored_assignments,
        horizon_days=defaults.gantt_horizon_days,
    )
    sensor_payload = build_sensor_payload(
        reconciled_records=[a.model_dump() for a in ha_window],
        daily_llm_briefing=daily_briefing,
    )
    mqtt_publisher.publish_student_state(
        student_id=student.id,
        display_name=student.display_name,
        object_id=student.outputs.home_assistant.sensor_object_id,
        device_name=student.outputs.home_assistant.device_name,
        payload=sensor_payload,
    )

    logger.info("=== Completed pipeline run for student '%s' ===", student.id)


def run_all_students(config_path: Path) -> None:
    """Load configuration and execute the pipeline for all enabled students."""
    cfg = load_app_config(config_path)
    state_dir = Path(os.getenv("STATE_OUTPUT_DIR", "/app/data"))

    aspen_runtime = AspenRuntimeConfig(
        base_url=os.getenv("ASPEN_BASE_URL", "https://tn-knox.myfollett.com/aspen/logon.do"),
        headless=os.getenv("ASPEN_HEADLESS", "true").lower() == "true",
        timeout_ms=int(os.getenv("ASPEN_TIMEOUT_MS", "30000")),
        throttle_seconds=float(os.getenv("ASPEN_THROTTLE_SECONDS", "1.5")),
        debug_scraper=os.getenv("DEBUG_SCRAPER", "false").lower() == "true",
        debug_dir=state_dir / "debug" / "aspen",
    )

    mqtt_config = MQTTConfig(
        host=os.getenv("MQTT_HOST", "bigdata"),
        port=int(os.getenv("MQTT_PORT", "1883")),
        username=os.getenv("MQTT_USERNAME") or None,
        password=os.getenv("MQTT_PASSWORD") or None,
        discovery_prefix=os.getenv("MQTT_DISCOVERY_PREFIX", "homeassistant"),
        retain=os.getenv("MQTT_RETAIN", "true").lower() == "true",
    )
    mqtt_publisher = HomeAssistantMQTTPublisher(mqtt_config)

    for student in cfg.students:
        if not student.enabled:
            logger.info("Skipping disabled student '%s'", student.id)
            continue
        try:
            process_student(
                student=student,
                defaults=cfg.global_defaults,
                aspen_runtime=aspen_runtime,
                mqtt_publisher=mqtt_publisher,
                state_dir=state_dir,
            )
        except Exception as exc:
            logger.exception("Unhandled error processing student '%s': %s", student.id, exc)


# ==============================================================================
# Daily Scheduler & CLI Entrypoint
# ==============================================================================


def run_scheduler(config_path: Path, schedule_hhmm: str = "06:00") -> None:
    """Lightweight daily daemon loop with graceful SIGTERM/SIGINT shutdown."""
    stop_requested = False

    def _handle_signal(signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        logger.info("Received signal %s; shutting down scheduler cleanly.", signum)
        stop_requested = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    last_run_date: str | None = None
    logger.info("Scheduler active; daily sync scheduled at %s local time", schedule_hhmm)

    while not stop_requested:
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        current_hhmm = now.strftime("%H:%M")

        if current_hhmm == schedule_hhmm and last_run_date != today_str:
            last_run_date = today_str
            run_all_students(config_path)

        time.sleep(20)


def main() -> None:
    load_dotenv()
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))

    parser = argparse.ArgumentParser(
        description="Canvas + Aspen Academic Tracker & Home Assistant Sync"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/app/config/students.yaml"),
        help="Path to students.yaml configuration file",
    )
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Execute the pipeline immediately once and exit",
    )
    parser.add_argument(
        "--schedule-time",
        type=str,
        default=os.getenv("SYNC_SCHEDULE_TIME", "06:00"),
        help="Daily local HH:MM run time when running in daemon mode (default: 06:00)",
    )
    args = parser.parse_args()

    if args.run_now:
        run_all_students(args.config)
    else:
        run_scheduler(args.config, schedule_hhmm=args.schedule_time)


if __name__ == "__main__":
    main()