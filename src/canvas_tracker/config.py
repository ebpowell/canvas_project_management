"""Configuration models and loaders for Canvas Tracker."""

from pathlib import Path
from typing import List, Optional
import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class StudentConfig(BaseModel):
    """Configuration for an individual student."""

    name: str
    canvas_domain: str
    canvas_token: str
    canvas_scraper_bin: str = "canvas-scraper"
    google_sheet_tab: str
    mqtt_sensor_suffix: str
    excluded_courses: List[str] = Field(default_factory=list)
    active_days_lookahead: int = 30
    active_days_lookback: int = 14


class StudentsFile(BaseModel):
    """Schema for the students.yaml configuration file."""

    students: List[StudentConfig] = Field(default_factory=list)


class AppSettings(BaseSettings):
    """Application-wide settings loaded from environment or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    log_level: str = "INFO"
    config_path: Path = Path("config/students.yaml")
    secrets_path: Path = Path("secrets/google-service-account.json")
    data_dir: Path = Path("data")
    run_schedule: str = "06:00"

    # Google Sheets
    google_spreadsheet_id_or_name: str = "Canvas_Student_Assignments"

    # MQTT (Homelab broker on bigdata)
    mqtt_host: str = "bigdata"
    mqtt_port: int = 1883
    mqtt_user: Optional[str] = None
    mqtt_password: Optional[str] = None
    mqtt_discovery_prefix: str = "homeassistant"


def load_students_config(path: Path) -> List[StudentConfig]:
    """Load and validate students configuration from a YAML file."""
    if not path.exists():
        raise FileNotFoundError(f"Students config file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    parsed = StudentsFile.model_validate(data)
    return parsed.students
