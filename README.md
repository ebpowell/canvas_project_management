# Canvas Project Management

Canvas Project Management is a consolidated Python homelab pipeline for collecting student assignments from Canvas LMS and Follett Aspen SIS, reconciling gradebook records with fuzzy matching, calculating risk and urgency scores, syncing to Google Sheets, and publishing rich entity and attribute states to Home Assistant through MQTT.

## Features

- **Multi-Source Extraction**:
  - Canvas assignment extraction via the `jdansak/Canvas-Scraper` Go CLI with a direct Canvas LMS REST API fallback and sample fixture generation.
  - Follett Aspen SIS gradebook extraction via Playwright headless browser automation (fault-isolated).
- **Fuzzy Reconciliation**:
  - Automatic cross-matching between Canvas LMS and Follett Aspen records using title token sorting and course/date-window correlation.
  - Identification of Aspen-only missing/zero entries and discrepancies.
- **Decision Engine & Prioritization**:
  - Multi-tier rule-based risk scoring (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`) and recommended actions (`ESCALATE_TEACHER_SYNC`, `PRIORITIZE_TONIGHT`, `SCHEDULE_THIS_WEEK`, `MONITOR`).
  - Optional LLM hook (Ollama / OpenAI-compatible API) for gray-zone match disambiguation and automated daily briefing summaries.
- **Dual Configuration Schema Support**:
  - Automatically loads and adapts both the simple legacy `students.yaml` and the comprehensive multi-student reconciliation schema (`homeassistant/Multi-Student Tracking, Reconciliation & AI Hook Configuration.yaml`).
  - Supports `${ENV_VAR}` placeholder interpolation and environment variable overrides.
- **Multi-Destination Synchronization**:
  - **Google Sheets**: Idempotently updates both `Raw_Data_<Student>` (10 columns with Gantt bounds and risk scores) and `Discrepancies_<Student>` (gradebook discrepancies with teacher inquiry drafts).
  - **Home Assistant (MQTT)**: Publishes MQTT Discovery sensors with active counts, 21-day Gantt timeline windows, top-priority action queues, and Aspen discrepancy counts matching Lovelace dashboards.
  - **Disk Snapshots**: Persists structured `state_<student_id>.json` snapshots for local agent tools and integrations.

---

## Architecture & Pipeline Flow

```text
students.yaml / Multi-Student YAML
                 │
                 ▼
         main.py / CLI
                 │
                 ▼
          sync.run_pipeline
                 │
   ┌─────────────┴─────────────┐
   ▼                           ▼
Stage 1: Canvas Scraper    Stage 2: Aspen SIS Scraper (Playwright)
(Go CLI + REST Fallback)       (Academics & Gradebook Parsing)
   │                           │
   └─────────────┬─────────────┘
                 ▼
      Stage 3: Assignment Reconciler
     (Fuzzy Match & Aspen Discrepancies)
                 │
                 ▼
      Stage 4: LLM Disambiguation (Optional)
    (Ollama / OpenAI Gray-Zone Matching)
                 │
                 ▼
      Stage 5: Decision Engine Scoring
    (Risk Weighting & Urgency Tiering)
                 │
                 ▼
      Stage 6: Multi-Output Export
  ┌──────────────┼──────────────┬──────────────┐
  ▼              ▼              ▼              ▼
State Disk     Google Sheets  Google Sheets   Home Assistant
Snapshot       Raw_Data Tab   Discrepancies   MQTT Discovery &
(JSON)         (10 Columns)   (Inquiries)     Sensor Attributes
```

---

## Project Layout

```text
canvas_project_management/
├── src/canvas_tracker/
│   ├── __init__.py             # Package metadata
│   ├── main.py                 # CLI entrypoint and daily daemon loop
│   ├── sync.py                 # 6-stage pipeline orchestrator
│   ├── config.py               # Dual v1/v2 Pydantic models & YAML/env loaders
│   ├── reconciler.py           # RapidFuzz / difflib matcher & UnifiedAssignment model
│   ├── decision_engine.py      # Rule-based / ML risk scoring and urgency tiering
│   ├── hooks.py                # Re-export bridge for decision and LLM agent hooks
│   ├── llm_agent.py            # Ollama / OpenAI structured LLM agent hook
│   ├── scraper.py              # Canvas Go CLI wrapper + REST API fallback
│   ├── aspen_scraper.py        # Playwright Follett Aspen SIS gradebook scraper
│   ├── sheets_client.py        # Google Sheets client (Raw_Data & Discrepancies tabs)
│   ├── mqtt_publisher.py       # Home Assistant MQTT Discovery & rich state publisher
│   ├── normalizer.py           # Date calculations (unlock_at, due_at, start_date)
│   └── prompts/                # Prompt templates for LLM agents & skills
├── homeassistant/
│   ├── lovelace_dashboard.yaml # ApexCharts Gantt cards, action queues, and alerts
│   ├── automations.yaml        # Notification automations
│   ├── Multi-Student Tracking, Reconciliation & AI Hook Configuration.yaml
│   └── ...
├── config/
│   ├── students.example.yaml   # Example student configuration
│   └── Canvas-Tracker & Aspen Reconciliation Environment Configuration.txt
├── tests/
│   ├── test_normalizer.py      # Tests for date parsing and status classification
│   └── test_reconciliation.py  # Tests for fuzzy matching, risk scoring, and MQTT payload
├── Dockerfile                  # Multi-stage image with Go scraper, Python, & Playwright
├── docker-compose.yml          # Container configuration with host-gateway resolution
└── pyproject.toml              # Unified package manifest (Hatchling)
```

---

## Configuration

Copy and customize the template files:

```bash
cp .env.example .env
cp config/students.example.yaml config/students.yaml
```

### Student Configuration Schemas

The system supports two YAML configuration styles:

#### 1. Multi-Student Reconciliation Schema (Recommended)
Defined in `homeassistant/Multi-Student Tracking, Reconciliation & AI Hook Configuration.yaml`:

```yaml
global_defaults:
  default_lead_days: 7
  gantt_horizon_days: 21
  reconciliation:
    fuzzy_match_threshold: 80
    fuzzy_gray_zone_min: 60
    due_date_window_days: 3
    low_grade_alert_threshold: 60.0
  decision_model:
    enabled: true
    provider: "rule_based"
  llm_agent:
    enabled: false

students:
  - id: "student_1"
    display_name: "Student One"
    enabled: true
    canvas:
      auth_type: "token"
      token: "${STUDENT_1_CANVAS_TOKEN}"
    aspen:
      enabled: true
      portal_type: "family"
      username: "${STUDENT_1_ASPEN_USERNAME}"
      password: "${STUDENT_1_ASPEN_PASSWORD}"
      course_map:
        "0301-1":
          canonical_name: "Algebra II"
    outputs:
      google_sheets:
        raw_data_tab: "Raw_Data_Student1"
        discrepancies_tab: "Discrepancies_Student1"
      home_assistant:
        sensor_object_id: "canvas_assignments_student1"
        device_name: "Canvas Tracker (Student One)"
```

#### 2. Legacy Schema
Supported seamlessly for backwards compatibility:

```yaml
students:
  - name: "Alex"
    canvas_domain: "school.instructure.com"
    canvas_token: "YOUR_CANVAS_API_TOKEN"
    canvas_scraper_bin: "canvas-scraper"
    google_sheet_tab: "Raw_Data_Alex"
    mqtt_sensor_suffix: "alex"
```

### Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `CONFIG_PATH` | `config/students.yaml` | Path to students configuration YAML |
| `SECRETS_PATH` | `secrets/google-service-account.json` | Google Cloud service account JSON path |
| `DATA_DIR` | `data` | Directory for raw snapshots and debug artifacts |
| `RUN_SCHEDULE` | `06:00` | Scheduled daily execution time in local time |
| `CANVAS_BASE_URL` | `https://knoxschools.instructure.com` | Base URL for Canvas LMS |
| `ASPEN_BASE_URL` | `https://tn-knox.myfollett.com/aspen/logon.do` | Login URL for Follett Aspen SIS |
| `ASPEN_HEADLESS` | `true` | Run Playwright in headless mode |
| `GOOGLE_SPREADSHEET_ID` | `Canvas_Student_Assignments` | Spreadsheet ID or name |
| `MQTT_HOST` | `bigdata` | MQTT broker hostname or IP |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USERNAME` | *(optional)* | MQTT username |
| `MQTT_PASSWORD` | *(optional)* | MQTT password |
| `MQTT_DISCOVERY_PREFIX` | `homeassistant` | Home Assistant MQTT discovery prefix |
| `LLM_BASE_URL` | `http://bigdata:11434/v1` | OpenAI-compatible endpoint (e.g. Ollama) |
| `LLM_MODEL` | `gpt-oss:20b` | Model name for agentic triage |

---

## Local Development & Usage

Requirements:
- Python 3.12+
- `uv` (or standard virtual environment)

### Running Tests

```bash
# Run the complete test suite (normalization, reconciliation, risk scoring, MQTT)
./.venv/bin/pytest -v
```

### CLI Commands

```bash
# Execute immediate scraping and reconciliation without writing to external services
uv run canvas-tracker --config config/students.yaml --dry-run

# Run full synchronization pipeline immediately and exit
uv run canvas-tracker --config config/students.yaml --run-now

# Run as a persistent background daemon scheduled at a specific time
uv run canvas-tracker --config config/students.yaml --schedule 06:00
```

---

## Docker Deployment

The project includes a multi-stage Docker build that bundles the Go-based `Canvas-Scraper` CLI, Python package dependencies, and Playwright Chromium:

```bash
# Build and start container
docker compose up -d --build

# View container logs
docker compose logs -f canvas-tracker
```

The container automatically executes an initial sync upon startup and then remains active as a scheduled daemon.
