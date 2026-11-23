from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ==============================================================================
# Pydantic Models Matching lovelace_dashboard.yaml & automations.yaml
# ==============================================================================


class DashboardAssignmentItem(BaseModel):
    """Schema consumed by apexcharts-card Gantt and Markdown action queue."""

    course: str
    name: str
    start: str  # YYYY-MM-DD
    end: str  # YYYY-MM-DD
    status: str  # e.g., "Upcoming", "Missing", "Submitted", "Aspen Only (Missing/0)"
    source: str  # "canvas" | "both" | "aspen_only"
    link: str = ""
    risk_score: float = 0.0
    urgency_tier: str = "MEDIUM"  # "CRITICAL" | "HIGH" | "MEDIUM" | "LOW"
    recommended_action: str = "MONITOR"


class AspenDiscrepancyItem(BaseModel):
    """Schema consumed by the conditional Aspen Discrepancy Banner & alert automation."""

    course: str
    name: str
    end: str  # YYYY-MM-DD due date in Aspen
    aspen_score: str  # e.g., "0", "0.0", "Missing", "M"
    inquiry_draft: str | None = None


class StudentSensorPayload(BaseModel):
    """Full JSON payload published to the Home Assistant MQTT state/attributes topic."""

    active_count: int
    missing_count: int
    aspen_discrepancy_count: int
    assignments: list[DashboardAssignmentItem] = Field(default_factory=list)
    aspen_discrepancies: list[AspenDiscrepancyItem] = Field(default_factory=list)
    top_priority_actions: list[DashboardAssignmentItem] = Field(default_factory=list)
    daily_llm_briefing: str | None = None
    last_synced_utc: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class MQTTConfig(BaseModel):
    """Broker connection settings loaded from .env."""

    host: str = "bigdata"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    discovery_prefix: str = "homeassistant"
    retain: bool = True
    qos: int = 1
    connect_timeout: int = 10


# ==============================================================================
# Home Assistant MQTT Discovery & State Publisher
# ==============================================================================


class HomeAssistantMQTTPublisher:
    """Publishes Home Assistant MQTT Discovery configs and retained student states."""

    def __init__(self, config: MQTTConfig) -> None:
        self.config = config

    def _build_client(self, client_suffix: str) -> mqtt.Client:
        """Create and configure a paho-mqtt v2 client instance."""
        client_id = f"canvas-tracker-{client_suffix}-{uuid.uuid4().hex[:6]}"
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
        )

        if self.config.username:
            client.username_pw_set(self.config.username, self.config.password)

        def on_connect(
            _client: mqtt.Client,
            _userdata: Any,
            _flags: mqtt.ConnectFlags,
            reason_code: mqtt.ReasonCode,
            _properties: mqtt.Properties | None,
        ) -> None:
            if reason_code.is_failure:
                logger.error("MQTT connection failed on %s: %s", self.config.host, reason_code)
            else:
                logger.debug("Connected to MQTT broker %s:%s", self.config.host, self.config.port)

        client.on_connect = on_connect
        return client

    def _discovery_topic(self, object_id: str) -> str:
        return f"{self.config.discovery_prefix}/sensor/{object_id}/config"

    def _state_topic(self, object_id: str) -> str:
        return f"{self.config.discovery_prefix}/sensor/{object_id}/state"

    def _build_discovery_payload(
        self,
        student_id: str,
        display_name: str,
        object_id: str,
        device_name: str,
    ) -> dict[str, Any]:
        """Construct Home Assistant MQTT Discovery configuration payload."""
        state_topic = self._state_topic(object_id)
        return {
            "name": f"{display_name} Assignments",
            "unique_id": f"canvas_tracker_{student_id}_assignments",
            "default_entity_id": f"sensor.{object_id}",
            "state_topic": state_topic,
            "json_attributes_topic": state_topic,
            "value_template": "{{ value_json.active_count }}",
            "unit_of_measurement": "assignments",
            "icon": "mdi:school",
            "force_update": True,
            "device": {
                "identifiers": [f"canvas_tracker_{student_id}"],
                "name": device_name,
                "manufacturer": "Homelab Academic Pipeline",
                "model": "Canvas + Aspen Reconciler",
                "sw_version": "1.0.0",
            },
        }

    def publish_student_state(
        self,
        *,
        student_id: str,
        display_name: str,
        object_id: str,
        device_name: str,
        payload: StudentSensorPayload,
    ) -> None:
        """Publish MQTT Discovery config and retained state/attributes to Home Assistant."""
        discovery_topic = self._discovery_topic(object_id)
        state_topic = self._state_topic(object_id)

        discovery_json = json.dumps(
            self._build_discovery_payload(
                student_id=student_id,
                display_name=display_name,
                object_id=object_id,
                device_name=device_name,
            ),
            ensure_ascii=False,
        )
        state_json = payload.model_dump_json()

        client = self._build_client(student_id)
        try:
            client.connect(
                self.config.host,
                self.config.port,
                keepalive=self.config.connect_timeout,
            )
            client.loop_start()

            # 1. Publish MQTT Discovery configuration
            disc_info = client.publish(
                topic=discovery_topic,
                payload=discovery_json,
                qos=self.config.qos,
                retain=self.config.retain,
            )
            disc_info.wait_for_publish(timeout=self.config.connect_timeout)

            # 2. Publish sensor state + JSON attributes payload
            state_info = client.publish(
                topic=state_topic,
                payload=state_json,
                qos=self.config.qos,
                retain=self.config.retain,
            )
            state_info.wait_for_publish(timeout=self.config.connect_timeout)

            logger.info(
                "Published Home Assistant MQTT state for %s (topic=%s, active=%d, discrepancies=%d)",
                student_id,
                state_topic,
                payload.active_count,
                payload.aspen_discrepancy_count,
            )
        finally:
            client.loop_stop()
            client.disconnect()


# ==============================================================================
# Helper to Assemble Payload from Reconciled & Prioritized Pipeline Records
# ==============================================================================


def build_sensor_payload(
    reconciled_records: list[dict[str, Any]],
    daily_llm_briefing: str | None = None,
) -> StudentSensorPayload:
    """Convert normalized/prioritized pipeline records into the exact HA payload."""
    dashboard_items: list[DashboardAssignmentItem] = []
    discrepancies: list[AspenDiscrepancyItem] = []

    for rec in reconciled_records:
        item = DashboardAssignmentItem(
            course=rec["course"],
            name=rec["name"],
            start=rec["start_date"],
            end=rec["due_date"],
            status=rec["status"],
            source=rec.get("source", "canvas"),
            link=rec.get("link", ""),
            risk_score=float(rec.get("risk_score", 0.0)),
            urgency_tier=rec.get("urgency_tier", "MEDIUM"),
            recommended_action=rec.get("recommended_action", "MONITOR"),
        )
        dashboard_items.append(item)

        if item.source == "aspen_only" and rec.get("is_discrepancy", False):
            discrepancies.append(
                AspenDiscrepancyItem(
                    course=item.course,
                    name=item.name,
                    end=item.end,
                    aspen_score=str(rec.get("aspen_score", "Missing")),
                    inquiry_draft=rec.get("inquiry_draft"),
                )
            )

    active_items = [
        i for i in dashboard_items if i.status not in {"Submitted", "Graded"}
    ]
    missing_items = [
        i for i in dashboard_items if i.status in {"Missing", "Late", "Aspen Only (Missing/0)"}
    ]
    top_priority = sorted(
        [i for i in active_items if i.urgency_tier in {"CRITICAL", "HIGH"}],
        key=lambda x: x.risk_score,
        reverse=True,
    )

    return StudentSensorPayload(
        active_count=len(active_items),
        missing_count=len(missing_items),
        aspen_discrepancy_count=len(discrepancies),
        assignments=dashboard_items,
        aspen_discrepancies=discrepancies,
        top_priority_actions=top_priority,
        daily_llm_briefing=daily_llm_briefing,
    )