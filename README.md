# canvas_project_management

Automated student assignment tracking pipeline that extracts assignments via Canvas, normalizes dates for Gantt visualization, syncs data to Google Sheets, and publishes state and attributes via MQTT to Home Assistant.

## Features
- **Automated Ingestion**: Scrapes assignments per student via `jdansak/Canvas-Scraper` CLI with Canvas LMS REST API fallback.
- **Gantt Date Normalization**: Converts Canvas timestamps to normalized `YYYY-MM-DD` bounds (`start_date` via `unlock_at` or `due_at - 7d`; `due_date` via `due_at`) and calculates status flags (`Missing`, `Late`, `Submitted`, `Graded`, `Upcoming`).
- **Google Sheets In-Place Sync**: Idempotently creates/updates a worksheet tab per student with columns: `Course`, `Assignment`, `Start Date`, `Due Date`, `Status`, `Weight/Pts`, `Link` without breaking linked formulas or timeline charts.
- **Home Assistant MQTT Discovery**: Auto-registers entities via MQTT discovery under `homeassistant/sensor/canvas_assignments_<student>` with retained attributes (`assignments` array) for instant Lovelace dashboards.
- **Homelab Ready**: Dockerized multi-stage container adhering to homelab standards targeting local Ubuntu servers (e.g. `bigdata`) with non-root runtime, structured JSON logging, and daily scheduled cron execution (`06:00`).

---

## Architecture & Directory Layout

```
canvas_project_management/
├── config/
│   └── students.example.yaml     # Student config template
├── data/                         # Local storage for scraper JSON payloads
├── homeassistant/
│   ├── lovelace_dashboard.yaml   # ApexCharts rangeBar Gantt & flex-table views
│   └── automations.yaml          # Sample push notification automations
├── secrets/                      # Ignored directory for Google service account JSON
├── src/
│   └── canvas_tracker/
│       ├── __init__.py
│       ├── config.py             # Pydantic settings & config parser
│       ├── main.py               # CLI entrypoint (--run-now, --schedule, --dry-run)
│       ├── mqtt_publisher.py     # Home Assistant MQTT discovery & state publisher
│       ├── normalizer.py         # Assignment validation & Gantt date normalization
│       ├── scraper.py            # Canvas-Scraper runner with REST API fallback
│       ├── sheets_client.py      # Idempotent Google Sheets gspread updater
│       └── sync.py               # Pipeline orchestrator
├── tests/
│   └── test_normalizer.py        # Pytest test suite
├── .env.example
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

---

## Setup Guide

### 1. Google Cloud Service Account Setup
1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create or select a project.
2. Enable the **Google Sheets API** and **Google Drive API** under **APIs & Services > Library**.
3. Go to **APIs & Services > Credentials** > **Create Credentials** > **Service Account**.
4. Create a JSON key for the service account and save it to:
   ```
   secrets/google-service-account.json
   ```
5. Create a Google Spreadsheet (e.g., named `Canvas_Student_Assignments`) and **Share** it with the service account's client email (found in the JSON key) granting **Editor** permissions.

### 2. Canvas API Token / Cookie Extraction
1. Log into your institution's Canvas LMS portal (e.g., `https://school.instructure.com`).
2. Go to **Account** > **Settings**.
3. Under **Approved Integrations**, click **+ New Access Token**.
4. Name the token `canvas-tracker` and generate it.
5. Copy the generated token into `config/students.yaml`.

### 3. Configure Students & Environment
Copy the example templates:
```bash
cp .env.example .env
cp config/students.example.yaml config/students.yaml
```
Edit `config/students.yaml` with each student's Canvas domain, token, target Google Sheet tab, and MQTT sensor suffix:
```yaml
students:
  - name: "Alex"
    canvas_domain: "school.instructure.com"
    canvas_token: "CANVAS_API_TOKEN"
    google_sheet_tab: "Raw_Data_Alex"
    mqtt_sensor_suffix: "alex"
```

---

## Local Development & Testing

Use `uv` to manage Python virtual environment and dependencies:
```bash
# Install dependencies
uv sync --extra dev

# Run test suite
uv run pytest

# Execute a dry-run test
uv run canvas-tracker --dry-run --config config/students.yaml

# Run an immediate sync against Google Sheets and Home Assistant
uv run canvas-tracker --run-now
```

---

## Deployment on Homelab Host (`bigdata`)

Deploy using Docker Compose:
```bash
# Build and start the container in detached mode
docker compose up -d --build

# Inspect structured JSON logs
docker compose logs -f canvas-tracker
```

The container automatically runs an initial sync on startup and schedules daily sync runs at `06:00` local time.

---

## Home Assistant Dashboard Integration

1. Ensure the Mosquitto MQTT broker is active on `bigdata` (port `1883`).
2. When `canvas-tracker` runs, it publishes MQTT discovery messages:
   - Topic: `homeassistant/sensor/canvas_assignments_<student>/config`
   - Entities auto-appear as `sensor.canvas_assignments_<student>`.
3. In Home Assistant Lovelace:
   - Import the cards from [`homeassistant/lovelace_dashboard.yaml`](file:///home/ebpowell/GIT_REPO/canvas_project_management/homeassistant/lovelace_dashboard.yaml).
   - Features include:
     - **ApexCharts Card**: Horizontal `rangeBar` Gantt view showing assignments by course, highlighting `Missing` items in red (`#e53935`) and upcoming items in blue (`#1e88e5`).
     - **Flex Table Card / Markdown Card**: Detailed list with direct clickable links to Canvas.
4. Import sample notifications from [`homeassistant/automations.yaml`](file:///home/ebpowell/GIT_REPO/canvas_project_management/homeassistant/automations.yaml) to receive mobile push alerts when assignments become missing.
