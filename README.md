# Canvas Project Management

Canvas Project Management is a Python homelab pipeline for collecting student assignments, normalizing them into Gantt-friendly dates, syncing them to Google Sheets, and publishing Home Assistant entities through MQTT.

The repository also contains an in-progress Canvas + Follett Aspen reconciliation design. The checked-in package entry point and primary Docker build currently run the Canvas-only pipeline described below.

## Current Runtime

- Python 3.12 package installed with `uv` and Hatchling
- Canvas assignment scraping through the `jdansak/Canvas-Scraper` Go CLI
- Direct Canvas LMS REST API fallback
- Pydantic models and YAML/Pydantic Settings configuration
- Google Sheets export through `gspread` and a Google service account
- Home Assistant MQTT discovery and retained sensor payloads through Paho MQTT
- Docker deployment with scheduled `06:00` execution
- Pytest unit tests for date and status normalization

## Architecture

```text
students.yaml
     |
     v
main.py / canvas-tracker
     |
     v
sync.run_pipeline
     |
     +--> scraper.run_scraper_cli
     |      |
     |      +--> Canvas-Scraper Go binary
     |      +--> Direct Canvas REST API fallback
     |
     +--> normalizer.normalize_assignment
     |
     +--> sheets_client.GoogleSheetsSync
     |      `--> one worksheet per configured student
     |
     `--> mqtt_publisher.MqttDiscoveryPublisher
            `--> Home Assistant MQTT discovery + retained state
```

### Processing stages

1. `main.py` loads environment settings and student configuration.
2. `sync.py` processes each enabled student in `students.yaml`.
3. `scraper.py` runs `canvas-scraper` when installed and falls back to Canvas REST API calls.
4. `normalizer.py` creates a consistent record containing:
   - Canvas course and assignment name
   - normalized `YYYY-MM-DD` start and due dates
   - status: `Missing`, `Late`, `Submitted`, `Graded`, or `Upcoming`
   - points/weight and the Canvas assignment link
5. `sheets_client.py` creates or updates a worksheet tab for the student.
6. `mqtt_publisher.py` publishes a discovery config, active assignment count, and assignment attributes.

Assignment start dates use `unlock_at` when available. Otherwise, they are calculated as seven days before `due_at`, with the current date used when the due date is less than seven days away. Overdue work is classified as `Missing`.

## Project Layout

```text
canvas_project_management/
├── src/canvas_tracker/
│   ├── main.py                 # Canvas-tracker CLI and scheduled daemon
│   ├── sync.py                 # Pipeline orchestration
│   ├── scraper.py              # Canvas-Scraper CLI + REST fallback
│   ├── normalizer.py           # Date/status normalization
│   ├── sheets_client.py        # Google Sheets synchronization
│   ├── mqtt_publisher.py       # Home Assistant MQTT discovery
│   ├── config.py               # App and student Pydantic models
│   ├── canvas_scaper.py        # Experimental unified pipeline
│   ├── Aspen_Scaper.py         # Aspen Playwright scraper prototype
│   ├── fuzzy_logic.py          # Reconciliation prototype
│   ├── decision_engine.py      # Risk/priority prototype
│   └── llm_agent.py            # Ollama/OpenAI-compatible agent prototype
├── homeassistant/
│   ├── lovelace_dashboard.yaml # Lovelace Gantt/table examples
│   ├── automations.yaml        # Notification examples
│   └── ...                     # Additional configuration examples
├── config/
│   └── students.example.yaml
├── tests/
│   └── test_normalizer.py
├── Dockerfile
├── Dockerfile_canvas_scrpaer
├── docker-compose.yml
├── compose_scaper.yaml
└── pyproject.toml
```

`pyproject.toml` is the single package manifest used by the project and Docker builds.

## Configuration

Copy the templates:

```bash
cp .env.example .env
cp config/students.example.yaml config/students.yaml
```

### Student configuration

`config/students.example.yaml` documents the active schema:

```yaml
students:
  - name: "Alex"
    canvas_domain: "school.instructure.com"
    canvas_token: "YOUR_CANVAS_API_TOKEN"
    canvas_scraper_bin: "canvas-scraper"
    google_sheet_tab: "Raw_Data_Alex"
    mqtt_sensor_suffix: "alex"
    excluded_courses: []
    active_days_lookahead: 30
    active_days_lookback: 14
```

The Canvas token can be an approved LMS API token. The REST fallback requires a valid token; if no valid token is present, `scraper.py` returns demonstration assignments instead of contacting Canvas.

Keep real student configuration and credentials out of Git:

- `config/students.yaml`
- `.env`
- `secrets/google-service-account.json`

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Python log level |
| `CONFIG_PATH` | `config/students.yaml` | Student YAML path |
| `SECRETS_PATH` | `secrets/google-service-account.json` | Google service-account JSON |
| `DATA_DIR` | `data` | Raw payload and state directory |
| `RUN_SCHEDULE` | `06:00` | Local-time daily run in scheduled mode |
| `GOOGLE_SPREADSHEET_ID_OR_NAME` | `Canvas_Student_Assignments` | Spreadsheet title or ID |
| `MQTT_HOST` | `bigdata` | MQTT broker hostname |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USER` | unset | Optional MQTT username |
| `MQTT_PASSWORD` | unset | Optional MQTT password |
| `MQTT_DISCOVERY_PREFIX` | `homeassistant` | MQTT discovery namespace |

Pydantic Settings accepts either environment variables or values from `.env`.

## Google Sheets Setup

1. Create or select a Google Cloud project.
2. Enable the Google Sheets API and Google Drive API.
3. Create a service account and download its JSON key.
4. Save the key as `secrets/google-service-account.json`.
5. Create a spreadsheet and share it with the service account's `client_email`.
6. Set `GOOGLE_SPREADSHEET_ID_OR_NAME` to the spreadsheet title or ID.

Each student tab is cleared and rewritten in-place with:

```text
Course | Assignment | Start Date | Due Date | Status | Weight/Pts | Link
```

Assignments are sorted by due date and then course. The implementation is intentionally designed to rewrite tabular values without recreating the worksheet.

## Local Development

Requirements:

- Python 3.12+
- `uv`
- Optional: Google credentials
- Optional: a Mosquitto/MQTT broker

Install and test:

```bash
uv sync --extra dev
uv run pytest
```

Run the local pipeline:

```bash
# Scrape and normalize, but do not write Sheets or publish MQTT
uv run canvas-tracker --config config/students.yaml --dry-run

# Scrape and immediately perform all external exports
uv run canvas-tracker --config config/students.yaml --run-now

# Run scheduled daemon mode; it performs an initial sync
uv run canvas-tracker --config config/students.yaml --schedule 06:00
```

## Docker Deployment

The primary `docker-compose.yml` build uses `Dockerfile`, copies the Canvas-Scraper Go binary into the Python 3.12 runtime, installs the package with `uv`, and runs as a non-root user.

```bash
docker compose up -d --build
docker compose logs -f canvas-tracker
```

The container performs an initial sync on startup, then schedules another run daily. Docker logging is limited to three 10 MB JSON log files.

`compose_scaper.yaml` is a host-oriented variant using `/opt/data/canvas-tracker/*`, `TZ`, `SYNC_SCHEDULE_TIME`, and an environment file. `Dockerfile_canvas_scrpaer` is a more feature-rich Playwright/Aspen-oriented image, but it is not used by `docker-compose.yml`.

## Home Assistant

When a sync succeeds, the MQTT publisher creates a sensor for each suffix:

- Discovery topic: `homeassistant/sensor/canvas_assignments_<suffix>/config`
- State topic: `homeassistant/sensor/canvas_assignments_<suffix>/state`
- Entity: `sensor.canvas_assignments_<suffix>`

The sensor state is the count of active `Missing` and `Upcoming` assignments. The retained JSON attributes contain:

- `student`
- `total_count`
- `missing_count`
- `upcoming_count`
- `assignments`

Each assignment attribute contains `id`, `course`, `name`, `start`, `end`, `status`, `points`, and `link`.

`homeassistant/lovelace_dashboard.yaml` contains ApexCharts Gantt and table examples. `homeassistant/automations.yaml` contains notification examples.

## In-Progress Canvas + Aspen Reconciliation

Several modules describe a broader reconciliation architecture:

- Canvas and Aspen normalized records
- RapidFuzz course/title matching
- LLM-assisted gray-zone disambiguation
- rule-based risk scoring and urgency tiers
- Aspen Missing/zero discrepancy detection
- Home Assistant discrepancy and briefing payloads
- persisted `state_<student_id>.json` agent tool snapshots

The legacy module names and imports are still being consolidated: for example, the package entry point is `main.py`, while `canvas_scaper.py` is an experimental alternate entry point and references modules not yet present under those names. Use the Canvas-only `canvas-tracker` command as the supported baseline until the reconciliation modules are wired into `main.py` and the active package manifest.

## Tests

The current test suite covers normalized start/due dates, status classification, payload conversion, and Google-Sheets/MQTT row shapes:

```bash
uv run pytest
```
