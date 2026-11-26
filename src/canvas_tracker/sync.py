"""Sync coordinator executing the multi-stage Canvas and Aspen reconciliation pipeline."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional

from canvas_tracker.aspen_scraper import (
    AspenRuntimeConfig,
    scrape_aspen_sync,
)
from canvas_tracker.config import (
    AppConfig,
    AppSettings,
    GlobalDefaultsConfig,
    StudentConfig,
    StudentEntryConfig,
    load_app_config,
    load_students_config,
)
from canvas_tracker.hooks import (
    AgenticLLMHook,
    build_decision_model,
)
from canvas_tracker.mqtt_publisher import (
    HomeAssistantMQTTPublisher,
    MQTTConfig,
    build_sensor_payload,
)
from canvas_tracker.reconciler import (
    AssignmentReconciler,
    UnifiedAssignment,
)
from canvas_tracker.scraper import run_canvas_scraper
from canvas_tracker.sheets_client import GoogleSheetsSync

logger = logging.getLogger(__name__)


def filter_gantt_horizon(
    assignments: List[UnifiedAssignment],
    horizon_days: int,
    reference_date: Optional[str] = None,
) -> List[UnifiedAssignment]:
    """Keep all Missing/Discrepancy items plus active work within the Gantt lookahead window."""
    ref = reference_date or datetime.now().strftime("%Y-%m-%d")
    now_dt = datetime.strptime(ref[:10], "%Y-%m-%d")
    cutoff_dt = now_dt + timedelta(days=horizon_days)
    past_grace_dt = now_dt - timedelta(days=7)

    filtered: List[UnifiedAssignment] = []
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
    sheets_client: Optional[GoogleSheetsSync],
    state_dir: Path,
    dry_run: bool = False,
) -> None:
    """Run all 6 stages for a single configured student."""
    logger.info("=== Starting pipeline run for student '%s' ===", student.id)

    canvas_base_url = os.getenv("CANVAS_BASE_URL", "https://knoxschools.instructure.com")

    # 1. Extract Canvas assignments
    canvas_records = run_canvas_scraper(
        student=student,
        canvas_base_url=canvas_base_url,
        default_lead_days=defaults.default_lead_days,
        output_dir=state_dir,
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
    if rec_result.gray_zone_candidates and defaults.llm_agent.enabled:
        confirmed_ids = llm_hook.resolve_gray_zone_matches(
            rec_result.gray_zone_candidates
        )
        unified = reconciler.apply_llm_match_resolutions(rec_result, confirmed_ids)
    else:
        unified = rec_result.unified_assignments

    # 5. Decision Engine Scoring & LLM Triage/Briefing
    decision_model = build_decision_model(defaults.decision_model)
    scored_assignments = decision_model.evaluate_batch(unified)

    if defaults.llm_agent.enabled:
        scored_assignments = llm_hook.triage_aspen_discrepancies(
            student_name=student.display_name,
            assignments=scored_assignments,
        )
        daily_briefing = llm_hook.generate_daily_briefing(
            student_name=student.display_name,
            assignments=scored_assignments,
        )
    else:
        daily_briefing = None

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

    if dry_run:
        logger.info("[Dry Run] Skipping Google Sheets and MQTT publishing for '%s'.", student.id)
        return

    # 6b. Sync full dataset to Google Sheets
    if sheets_client:
        try:
            sheets_client.sync_unified_assignments(
                student=student,
                assignments=scored_assignments,
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
    try:
        mqtt_publisher.publish_student_state(
            student_id=student.id,
            display_name=student.display_name,
            object_id=student.outputs.home_assistant.sensor_object_id,
            device_name=student.outputs.home_assistant.device_name,
            payload=sensor_payload,
        )
    except Exception as exc:
        logger.error("MQTT publish failed for '%s': %s", student.id, exc)

    logger.info("=== Completed pipeline run for student '%s' ===", student.id)


def run_pipeline(
    config_path: Optional[Path] = None,
    settings: Optional[AppSettings] = None,
    dry_run: bool = False,
) -> None:
    """Execute the multi-stage synchronization pipeline for all configured students."""
    app_settings = settings or AppSettings()
    cfg_file = config_path or app_settings.config_path

    logger.info("Starting student assignment sync pipeline using %s...", cfg_file)
    try:
        cfg = load_app_config(cfg_file)
    except Exception as e:
        logger.error("Failed to load configuration from %s: %s", cfg_file, e)
        return

    if not cfg.students:
        logger.warning("No students configured in %s.", cfg_file)
        return

    state_dir = app_settings.data_dir
    state_dir.mkdir(parents=True, exist_ok=True)

    aspen_runtime = AspenRuntimeConfig(
        base_url=os.getenv("ASPEN_BASE_URL", "https://tn-knox.myfollett.com/aspen/logon.do"),
        headless=os.getenv("ASPEN_HEADLESS", "true").lower() == "true",
        timeout_ms=int(os.getenv("ASPEN_TIMEOUT_MS", "30000")),
        throttle_seconds=float(os.getenv("ASPEN_THROTTLE_SECONDS", "1.5")),
        debug_scraper=os.getenv("DEBUG_SCRAPER", "false").lower() == "true",
        debug_dir=state_dir / "debug" / "aspen",
    )

    mqtt_config = MQTTConfig(
        host=os.getenv("MQTT_HOST", app_settings.mqtt_host),
        port=int(os.getenv("MQTT_PORT", str(app_settings.mqtt_port))),
        username=os.getenv("MQTT_USERNAME", app_settings.mqtt_user) or None,
        password=os.getenv("MQTT_PASSWORD", app_settings.mqtt_password) or None,
        discovery_prefix=os.getenv("MQTT_DISCOVERY_PREFIX", app_settings.mqtt_discovery_prefix),
        retain=os.getenv("MQTT_RETAIN", "true").lower() == "true",
    )
    mqtt_publisher = HomeAssistantMQTTPublisher(mqtt_config)

    sheets_id = os.getenv("GOOGLE_SPREADSHEET_ID", app_settings.google_spreadsheet_id_or_name)
    sa_path = Path(os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(app_settings.secrets_path)))
    sheets_client = GoogleSheetsSync(
        service_account_path=sa_path,
        spreadsheet_name_or_id=sheets_id,
    )

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
                sheets_client=sheets_client,
                state_dir=state_dir,
                dry_run=dry_run,
            )
        except Exception as exc:
            logger.exception("Unhandled error processing student '%s': %s", student.id, exc)

    logger.info("Assignment pipeline sync finished successfully.")
