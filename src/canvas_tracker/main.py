"""Command-line interface and daily daemon entrypoint for Canvas Tracker."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from dotenv import load_dotenv

from canvas_tracker.config import AppSettings
from canvas_tracker.sync import run_pipeline


def setup_logging(log_level: str) -> None:
    """Configure structured logging format."""
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format='{"timestamp": "%(asctime)s", "level": "%(levelname)s", "module": "%(name)s", "message": "%(message)s"}',
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def run_scheduler(settings: AppSettings, schedule_hhmm: str) -> None:
    """Lightweight daily daemon loop with graceful SIGTERM/SIGINT shutdown."""
    logger = logging.getLogger("canvas_tracker.scheduler")
    stop_requested = False

    def _handle_signal(signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        logger.info("Received signal %s; shutting down scheduler cleanly.", signum)
        stop_requested = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    last_run_date: str | None = None
    logger.info("Scheduler active; daily sync scheduled at %s local time", schedule_hhmm)

    # Initial run on container/service startup
    logger.info("Performing initial synchronization on startup...")
    run_pipeline(settings=settings, dry_run=False)

    while not stop_requested:
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        current_hhmm = now.strftime("%H:%M")

        if current_hhmm == schedule_hhmm and last_run_date != today_str:
            last_run_date = today_str
            logger.info("Triggering scheduled run for %s at %s", today_str, current_hhmm)
            run_pipeline(settings=settings, dry_run=False)

        time.sleep(20)


def main() -> None:
    """CLI entrypoint supporting --run-now, --schedule, and --dry-run flags."""
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Canvas + Aspen Academic Tracker & Home Assistant Sync"
    )
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Run sync pipeline immediately and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Execute scraping, normalization, and reconciliation without external Google Sheets / MQTT sync",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to students.yaml configuration file",
    )
    parser.add_argument(
        "--schedule",
        type=str,
        default=None,
        help="Daily run time in HH:MM format (e.g. '06:00')",
    )

    args = parser.parse_args()

    settings = AppSettings()
    if args.config:
        settings.config_path = args.config

    setup_logging(os.getenv("LOG_LEVEL", settings.log_level))
    logger = logging.getLogger("canvas_tracker.main")

    if args.run_now or args.dry_run:
        logger.info("Executing immediate run (dry_run=%s, config=%s)...", args.dry_run, settings.config_path)
        run_pipeline(settings=settings, dry_run=args.dry_run)
        sys.exit(0)

    # Scheduled daemon mode
    run_time = args.schedule or os.getenv("RUN_SCHEDULE") or settings.run_schedule
    logger.info("Starting Canvas Tracker daemon (daily at %s, config=%s)...", run_time, settings.config_path)
    run_scheduler(settings, run_time)


if __name__ == "__main__":
    main()
