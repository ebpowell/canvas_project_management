---
name: homelab-standards
description: Enforces local homelab architecture, Docker Compose packaging, Python uv dependency management, PostgreSQL persistence, and Home Assistant MQTT Discovery standards when scaffolding or modifying services deployed on the Ubuntu Server host bigdata.
---

# Goal
Ensure all newly generated or refactored services adhere strictly to self-hosted, containerized homelab standards targeting the `bigdata` server and Home Assistant instance without introducing proprietary SaaS dependencies or legacy packaging patterns.

# Instructions

## 1. Python Environment & Packaging (`uv`)
- Always manage Python projects with `uv` using a PEP 621-compliant `pyproject.toml` and committed `uv.lock`.
- Never generate `requirements.txt`, `setup.py`, `Pipenv`, or `poetry` configurations unless explicitly requested.
- Target Python 3.12+ and validate inputs/payloads using `pydantic` v2 models.
- Structure application code under `src/<package_name>/` with a clean CLI entrypoint and `pytest` unit tests under `tests/`.

## 2. Docker & Docker Compose Standards (`bigdata`)
- Package all services using multi-stage `Dockerfile` builds:
  - Builder stage: Install `uv`, copy `pyproject.toml` and `uv.lock`, and run `uv sync --frozen --no-dev --no-install-project`.
  - Runtime stage: Use `python:3.12-slim`, run as a non-root user (`appuser`), and copy only the `.venv` and `src/` directory.
- Write `docker-compose.yml` using modern Compose Specification syntax (omit the obsolete top-level `version:` key).
- Include explicit `restart: unless-stopped`, `healthcheck`, structured JSON logging (`json-file` driver with `max-size: "10m"` and `max-file: "3"`), and bind mounts mapped to `/opt/data/<service_name>/...`.
- Never hardcode secrets, tokens, or passwords in `Dockerfile` or `docker-compose.yml`; load all credentials via `.env` and provide a complete `.env.example`.

## 3. Home Assistant Integration (MQTT Discovery)
- Default MQTT broker hostname to `bigdata` (`MQTT_HOST=bigdata`, `MQTT_PORT=1883`) configurable via environment variables.
- Register entities dynamically via **Home Assistant MQTT Discovery** (`homeassistant/<component>/<node_id>/<object_id>/config`) with a unique `unique_id` and `device` block rather than requiring manual `configuration.yaml` sensor definitions.
- Publish state and JSON attribute payloads with `retain=True` so sensor states survive Home Assistant container restarts.

## 4. Database & Persistence
- When relational persistence is required, default to **PostgreSQL** (`psycopg` v3 or `SQLAlchemy` 2.0+) rather than SQLite or cloud-hosted databases.

# Verification Checklist
Before completing any task that triggers this skill:
1. Run `uv sync` and `uv run pytest` to confirm clean dependency resolution and passing tests.
2. Validate `docker-compose.yml` syntax via `docker compose config --quiet` (if Docker is available in the environment).
3. Confirm `.env.example` documents every environment variable referenced in `docker-compose.yml` and Python settings.

# Constraints
- Do not recommend or configure cloud SaaS monitoring, proprietary PaaS hosting, or managed third-party databases.
- Do not leave plaintext API tokens, session cookies, or service account JSON files outside ignored `secrets/` directories.