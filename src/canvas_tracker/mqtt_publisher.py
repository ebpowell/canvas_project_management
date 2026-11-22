"""Home Assistant MQTT Discovery and state/attributes publisher."""

import json
import logging
from typing import Any, Dict, List, Optional
import paho.mqtt.client as mqtt

from canvas_tracker.normalizer import NormalizedAssignment

logger = logging.getLogger(__name__)


class MqttDiscoveryPublisher:
    """Publishes Home Assistant MQTT Discovery payloads and state/attributes."""

    def __init__(
        self,
        host: str = "bigdata",
        port: int = 1883,
        username: Optional[str] = None,
        password: Optional[str] = None,
        discovery_prefix: str = "homeassistant",
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.discovery_prefix = discovery_prefix
        self._client: Optional[mqtt.Client] = None

    def _get_client(self) -> mqtt.Client:
        """Create and configure Paho MQTT client."""
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if self.username and self.password:
            client.username_pw_set(self.username, self.password)
        return client

    def publish_student_sensor(
        self,
        student_name: str,
        sensor_suffix: str,
        assignments: List[NormalizedAssignment],
    ) -> bool:
        """
        Register entity via MQTT discovery and publish state + attributes.
        State = total count of active assignments (Missing + Upcoming).
        Attributes = JSON array of structured assignments for Lovelace cards.
        """
        sensor_id = f"canvas_assignments_{sensor_suffix.lower()}"
        config_topic = f"{self.discovery_prefix}/sensor/{sensor_id}/config"
        state_topic = f"{self.discovery_prefix}/sensor/{sensor_id}/state"
        attr_topic = f"{self.discovery_prefix}/sensor/{sensor_id}/attributes"

        # Count active/actionable items
        missing_count = sum(1 for a in assignments if a.status == "Missing")
        upcoming_count = sum(1 for a in assignments if a.status == "Upcoming")
        active_count = missing_count + upcoming_count

        # Home Assistant MQTT Discovery Configuration payload
        discovery_payload = {
            "name": f"Canvas Assignments ({student_name})",
            "unique_id": sensor_id,
            "state_topic": state_topic,
            "json_attributes_topic": attr_topic,
            "icon": "mdi:school",
            "unit_of_measurement": "assignments",
            "device": {
                "identifiers": [f"canvas_tracker_{sensor_suffix.lower()}"],
                "name": f"Canvas Tracker ({student_name})",
                "model": "Canvas LMS Homelab Scraper",
                "manufacturer": "Canvas Homelab",
                "sw_version": "0.1.0",
            },
        }

        # Attributes array matching apexcharts-card requirements
        attributes_payload = {
            "student": student_name,
            "total_count": len(assignments),
            "missing_count": missing_count,
            "upcoming_count": upcoming_count,
            "assignments": [a.to_mqtt_item() for a in assignments],
        }

        try:
            client = self._get_client()
            client.connect(self.host, self.port, keepalive=60)
            client.loop_start()

            # Publish config, state, and attributes with retain=True
            client.publish(config_topic, json.dumps(discovery_payload), qos=1, retain=True)
            client.publish(state_topic, str(active_count), qos=1, retain=True)
            client.publish(attr_topic, json.dumps(attributes_payload), qos=1, retain=True)

            client.loop_stop()
            client.disconnect()

            logger.info(
                "Published MQTT state (%d active) and %d assignment attributes for %s to %s",
                active_count,
                len(assignments),
                student_name,
                self.host,
            )
            return True
        except Exception as e:
            logger.error("MQTT publish failed for %s to host %s: %s", student_name, self.host, e)
            return False
