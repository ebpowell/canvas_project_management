# canvas_project_management

Tools for tracking student work in the Canvas LMS platform.

Automated student assignment tracking pipeline that extracts assignments via Canvas, normalizes dates for Gantt visualization, syncs data to Google Sheets, and publishes state and attributes via MQTT to Home Assistant.

## Overview
- **Pipeline:** Canvas Scraper & tracker containerized service.
- **Data Normalization:** Parses Canvas assignments, computes start/due dates for timeline charts.
- **Destinations:**
  - **Google Sheets:** Tab per student for Timeline views and Stacked Bar Gantt charts.
  - **Home Assistant:** MQTT Discovery sensors reporting upcoming and missing assignments.
