"""Configuration models and loaders for Canvas Tracker."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, List, Literal, Optional
import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from canvas_tracker.aspen_scraper import AspenStudentConfig
from canvas_tracker.hooks import DecisionModelConfig, LLMAgentConfig
from canvas_tracker.reconciler import ReconciliationConfig

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _interpolate_env(obj: Any) -> Any:
    """Recursively expand ${VAR_NAME} placeholders in loaded YAML structures."""
    if isinstance(obj, str):
        return _ENV_PATTERN.sub(lambda m: os.getenv(m.group(1), ""), obj)
    if isinstance(obj, list):
        return [_interpolate_env(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _interpolate_env(v) for k, v in obj.items()}
    return obj


# ==============================================================================
# Multi-Student & Reconciliation Configuration (v2 Schema)
# ==============================================================================


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
    aspen: AspenStudentConfig = Field(default_factory=AspenStudentConfig)
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


# ==============================================================================
# Legacy Student Configuration (v1 Schema Support)
# ==============================================================================


class StudentConfig(BaseModel):
    """Legacy configuration for an individual student."""

    name: str
    canvas_domain: str
    canvas_token: str
    canvas_scraper_bin: str = "canvas-scraper"
    google_sheet_tab: str
    mqtt_sensor_suffix: str
    excluded_courses: List[str] = Field(default_factory=list)
    active_days_lookahead: int = 30
    active_days_lookback: int = 14

    def to_student_entry(self) -> StudentEntryConfig:
        """Convert a v1 student record to a unified v2 StudentEntryConfig."""
        student_id = self.mqtt_sensor_suffix.strip() or self.name.lower().replace(" ", "_")
        return StudentEntryConfig(
            id=student_id,
            display_name=self.name,
            enabled=True,
            canvas=CanvasStudentConfig(
                auth_type="token",
                token=self.canvas_token,
                cookie="",
                include_concluded_courses=False,
            ),
            aspen=AspenStudentConfig(enabled=False),
            outputs=StudentOutputsConfig(
                google_sheets=GoogleSheetsOutputConfig(
                    raw_data_tab=self.google_sheet_tab,
                    discrepancies_tab=f"Discrepancies_{self.name}",
                ),
                home_assistant=HomeAssistantOutputConfig(
                    sensor_object_id=f"canvas_assignments_{self.mqtt_sensor_suffix}",
                    device_name=f"Canvas Tracker ({self.name})",
                ),
            ),
        )


class StudentsFile(BaseModel):
    """Schema for legacy students.yaml configuration file."""

    students: List[StudentConfig] = Field(default_factory=list)


# ==============================================================================
# Application-wide Settings & Loaders
# ==============================================================================


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


def load_app_config(config_path: Path) -> AppConfig:
    """
    Load students.yaml, resolve .env placeholders, overlay .env overrides,
    and seamlessly adapt either v2 or v1 schema.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    raw_yaml = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    interpolated = _interpolate_env(raw_yaml)

    # Detect if file uses legacy v1 format (list of StudentConfig without global_defaults or id field)
    students_raw = interpolated.get("students", [])
    if students_raw and isinstance(students_raw, list) and "id" not in students_raw[0]:
        # Legacy v1 format
        legacy_students = [StudentConfig.model_validate(s) for s in students_raw]
        cfg = AppConfig(
            global_defaults=GlobalDefaultsConfig(),
            students=[s.to_student_entry() for s in legacy_students],
        )
    else:
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


def load_students_config(path: Path) -> List[StudentConfig]:
    """Legacy helper: Load and validate students configuration as List[StudentConfig]."""
    if not path.exists():
        raise FileNotFoundError(f"Students config file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    parsed = StudentsFile.model_validate(data)
    return parsed.students
