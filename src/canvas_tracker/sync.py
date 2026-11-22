"""Sync coordinator executing the full scraping, normalization, and export pipeline."""

import logging
from pathlib import Path
from typing import List

from canvas_tracker.config import AppSettings, StudentConfig, load_students_config
from canvas_tracker.mqtt_publisher import MqttDiscoveryPublisher
from canvas_tracker.normalizer import normalize_assignment
from canvas_tracker.scraper import run_scraper_cli
from canvas_tracker.sheets_client import GoogleSheetsSync

logger = logging.getLogger(__name__)


def run_pipeline(settings: AppSettings, dry_run: bool = False) -> None:
    """Execute the daily synchronization pipeline for all configured students."""
    logger.info("Starting Canvas assignment sync pipeline...")

    try:
        students = load_students_config(settings.config_path)
    except Exception as e:
        logger.error("Failed to load students configuration: %s", e)
        return

    if not students:
        logger.warning("No students configured in %s.", settings.config_path)
        return

    sheets_sync = GoogleSheetsSync(
        service_account_path=settings.secrets_path,
        spreadsheet_name_or_id=settings.google_spreadsheet_id_or_name,
    )

    mqtt_pub = MqttDiscoveryPublisher(
        host=settings.mqtt_host,
        port=settings.mqtt_port,
        username=settings.mqtt_user,
        password=settings.mqtt_password,
        discovery_prefix=settings.mqtt_discovery_prefix,
    )

    for student in students:
        logger.info("--- Processing student: %s ---", student.name)

        # 1. Scrape assignments
        raw_items = run_scraper_cli(student, output_dir=settings.data_dir)
        logger.info("Retrieved %d raw items for %s", len(raw_items), student.name)

        # 2. Normalize and compute Gantt bounds
        normalized = [normalize_assignment(item) for item in raw_items]
        missing = sum(1 for a in normalized if a.status == "Missing")
        upcoming = sum(1 for a in normalized if a.status == "Upcoming")
        logger.info(
            "Normalized %d assignments for %s (Missing: %d, Upcoming: %d)",
            len(normalized),
            student.name,
            missing,
            upcoming,
        )

        if dry_run:
            logger.info("[Dry Run] Skipping Google Sheets and MQTT publishing.")
            continue

        # 3. Update Google Sheets
        sheets_sync.sync_student_tab(student.google_sheet_tab, normalized)

        # 4. Publish MQTT Discovery & State to Home Assistant
        mqtt_pub.publish_student_sensor(
            student_name=student.name,
            sensor_suffix=student.mqtt_sensor_suffix,
            assignments=normalized,
        )

    logger.info("Canvas assignment pipeline sync finished successfully.")
