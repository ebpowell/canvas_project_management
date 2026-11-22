"""Command-line interface entrypoint for Canvas Tracker."""

import argparse
import logging
import sys
import time
from pathlib import Path
import schedule
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


def main() -> None:
    """CLI entrypoint supporting --run-now, --schedule, and --dry-run flags."""
    load_dotenv()

    parser = argparse.ArgumentParser(description="Canvas Tracker Pipeline")
    parser.add_argument("--run-now", action="store_true", help="Run sync pipeline immediately and exit")
    parser.add_argument("--dry-run", action="store_true", help="Execute scraping/normalization without external sync")
    parser.add_argument("--config", type=Path, default=None, help="Path to students.yaml config")
    parser.add_argument("--schedule", type=str, default=None, help="Daily run time (e.g. '06:00')")

    args = parser.parse_args()

    settings = AppSettings()
    if args.config:
        settings.config_path = args.config

    setup_logging(settings.log_level)
    logger = logging.getLogger("canvas_tracker.main")

    if args.run_now or args.dry_run:
        logger.info("Executing immediate run (dry_run=%s)...", args.dry_run)
        run_pipeline(settings, dry_run=args.dry_run)
        sys.exit(0)

    # Scheduled mode
    run_time = args.schedule or settings.run_schedule
    logger.info("Starting Canvas Tracker in scheduled daemon mode (daily at %s)...", run_time)

    # Initial run on container startup
    run_pipeline(settings, dry_run=False)

    schedule.every().day.at(run_time).do(run_pipeline, settings=settings, dry_run=False)

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Shutting down Canvas Tracker daemon.")
        sys.exit(0)


if __name__ == "__main__":
    main()
