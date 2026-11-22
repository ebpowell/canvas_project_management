# Role & Objective
Act as a Principal Data & DevOps Engineer. Build a containerized, automated daily student assignment tracking pipeline (`canvas-tracker`) that extracts assignments via `jdansak/Canvas-Scraper`, normalizes dates for Gantt visualization, syncs data to Google Sheets, and publishes state/attributes via MQTT to Home Assistant running on the local host `bigdata`.

# Architecture & Tech Stack
- **Runtime & Packaging:** Python 3.12+ managed with `uv` (`pyproject.toml` and `uv.lock`), packaged into a lightweight multi-stage Docker container (`docker-compose.yml`).
- **Scraper Engine:** `jdansak/Canvas-Scraper` (Go binary compiled or downloaded inside the container image) executed in headless `--json` CLI mode.
- **Data Normalization & Sync (`src/canvas_tracker/sync.py`):**
  - Parses JSON output from `Canvas-Scraper`.
  - Normalizes dates into `YYYY-MM-DD`: `start_date` uses `unlock_at` if available, falling back to `due_at - 7 days` (or today's date if `due_at` is within 7 days). `due_date` uses `due_at`.
  - Supports multi-student tracking via a `students.yaml` config file (mapping student names to Canvas tokens/cookies, target Google Sheet tabs, and MQTT sensor suffixes).
- **Output 1 — Google Sheets (`gspread`):**
  - Authenticates via a Google Service Account JSON file mounted at `/app/secrets/google-service-account.json`.
  - Overwrites a dedicated tab per student (e.g., `Raw_Data_<Student>`) in place with columns: `Course`, `Assignment`, `Start Date`, `Due Date`, `Status`, `Weight/Pts`, `Link` so linked Google Sheets Timeline views and Stacked Bar Gantt charts update without breaking cell references.
- **Output 2 — Home Assistant on `bigdata` (`paho-mqtt`):**
  - Publishes retained JSON payloads (`retain=True`) to `homeassistant/sensor/canvas_assignments_<student>/state` (state = count of upcoming/missing assignments; attributes = structured `assignments` array with `course`, `name`, `start`, `end`, `status`, `link`).
  - Defaults MQTT broker host to `bigdata` (configurable via `.env`).
- **Scheduling:** Built-in lightweight scheduler or container entrypoint cron running daily at `06:00` local time, plus a `--run-now` CLI flag for immediate execution and testing.

# Required Deliverables
Create the complete repository structure and generate all code, configuration templates, and Home Assistant YAML artifacts:

1. **Project Scaffolding & Dependencies:**
   - `pyproject.toml` configured for `uv` (`gspread`, `google-auth`, `paho-mqtt`, `pyyaml`, `python-dotenv`, `pydantic`).
   - `.env.example` and `config/students.example.yaml`.
   - `Dockerfile` (multi-stage: builds/installs `Canvas-Scraper` Go binary, sets up `uv` Python environment) and `docker-compose.yml` ready to deploy on `bigdata` with volume mounts for `./config`, `./secrets`, and `./data`.

2. **Core Python Pipeline (`src/canvas_tracker/`):**
   - `scraper.py`: Subprocess wrapper invoking `canvas-scraper --json` per student with proper error handling and timeout protection.
   - `normalizer.py`: Pydantic models validating assignment records and computing Gantt `start_date` / `due_date` bounds and status flags (`Missing`, `Late`, `Submitted`, `Graded`, `Upcoming`).
   - `sheets_client.py`: Idempotent `gspread` updater that creates the student tab if missing, formats header rows, and batch-updates rows cleanly.
   - `mqtt_publisher.py`: Publishes Home Assistant MQTT Discovery payloads (`homeassistant/sensor/canvas_assignments_<student>/config`) alongside the state/attributes payload so entities auto-register in Home Assistant without manual `configuration.yaml` edits.
   - `main.py`: CLI entrypoint supporting `--run-now` and scheduled daily execution, with structured JSON logging.

3. **Home Assistant Dashboard Artifacts (`homeassistant/`):**
   - `lovelace_dashboard.yaml`: A ready-to-import Lovelace view featuring:
     - A `custom:apexcharts-card` configured as a horizontal `rangeBar` Gantt chart grouped by course, coloring `Missing` items red (`#e53935`) and upcoming items blue (`#1e88e5`).
     - A `custom:flex-table-card` (and a zero-HACS fallback `markdown` card) listing assignments sorted by due date with clickable Canvas links.
   - `automations.yaml`: Sample Home Assistant automation notifying mobile devices when an assignment transitions to `Missing` or is due within 24 hours.

4. **Documentation (`README.md`):**
   - Step-by-step setup guide covering Google Cloud Service Account creation, Canvas token/cookie extraction, deploying the stack via Docker Compose on `bigdata`, and importing the Lovelace dashboard.

# Execution & Verification Plan
Before finishing:
1. Run `uv sync` and verify there are no dependency or syntax errors.
2. Write unit tests (`tests/test_normalizer.py`) with sample `Canvas-Scraper` JSON payloads covering edge cases (missing `unlock_at`, overdue assignments, null `due_at`) and run `uv run pytest`.
3. Validate the `Dockerfile` and `docker-compose.yml` syntax.