"""Home Assistant discovery without any MQTT command consumers."""

import json
import re
from collections.abc import Mapping
from typing import Any

from .models import EntityState, PanelInfo

FAULTS = {
    "2": ("Phone line failure", "connectivity", False),
    "4": ("CRC failure in panel configuration", "problem", False),
    "8": ("Battery", "battery", True),
    "16": ("Battery missing", "problem", True),
    "32": ("AC failure", "problem", True),
    "128": ("Communication failure since last RPS connection", "problem", False),
    "256": ("SDI failure since last RPS connection", "problem", False),
    "512": ("User code tamper since last RPS connection", "problem", False),
    "1024": ("Failure to call RPS since last RPS connection", None, False),
    "8192": ("Point bus failure since last RPS connection", "problem", False),
    "16384": ("Log overflow", "problem", False),
    "32768": ("Log threshold reached", "problem", False),
}


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _json(value: Any) -> str:
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _token(value: str) -> str:
    # A reversible encoding avoids collisions from punctuation, case, or Unicode folding.
    return "x" + value.encode("utf-8").hex()


def _template(field: str, binary: bool = False) -> str:
    value = "value_json." + field
    if binary:
        return "{{ 'None' if " + value + " is none else ('ON' if " + value + " else 'OFF') }}"
    return "{{ 'None' if " + value + " is none else " + value + " }}"


class DiscoveryBuilder:
    """Build retained discovery; the runtime publishes states and handles tombstones."""

    def __init__(self, *, gateway_id: str, topic_prefix: str, discovery_prefix: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", gateway_id):
            raise ValueError("gateway_id must be an ASCII MQTT discovery identifier")
        for prefix in (topic_prefix, discovery_prefix):
            if (
                not prefix
                or any(char in prefix for char in "+#\x00")
                or prefix.startswith("/")
                or prefix.endswith("/")
            ):
                raise ValueError("Invalid MQTT prefix")
        self.gateway_id = gateway_id
        self.topic_prefix = topic_prefix
        self.discovery_prefix = discovery_prefix

    def state_topic(self, state: EntityState) -> str:
        if state.kind not in {"area", "point", "door", "output", "fault"}:
            raise ValueError("Unsupported entity kind")
        return f"{self.topic_prefix}/state/{state.kind}/{_token(state.id)}"

    @staticmethod
    def state_payload(state: EntityState) -> str:
        return _json({"state": state.state, "attributes": state.attributes})

    def build(self, info: PanelInfo, states: tuple[EntityState, ...]) -> dict[str, str]:
        if not info.serial:
            raise ValueError("Panel serial is required for stable identity")
        root_id = f"bosch_mqtt_{self.gateway_id}_{_token(info.serial)}"
        panel_device = {
            "identifiers": [root_id],
            "name": f"Bosch {info.model}",
            "manufacturer": "Bosch Security Systems",
            "model": info.model,
            "serial_number": info.serial,
        }
        if info.firmware:
            panel_device["sw_version"] = info.firmware
        result: dict[str, str] = {}
        seen: set[tuple[str, str]] = set()
        for state in states:
            if (state.kind, state.id) in seen:
                raise ValueError("Duplicate entity identity")
            seen.add((state.kind, state.id))
            state_topic = self.state_topic(state)
            object_id = f"{_token(info.serial)}_{state.kind}_{_token(state.id)}"
            device = (
                panel_device
                if state.kind == "fault"
                else {
                    "identifiers": [f"{root_id}_{state.kind}_{_token(state.id)}"],
                    "name": state.name,
                    "manufacturer": "Bosch Security Systems",
                    "via_device": root_id,
                }
            )

            def add(
                domain: str,
                suffix: str = "",
                name: str | None = None,
                field: str = "state",
                *,
                object_id: str = object_id,
                device: dict = device,
                state_topic: str = state_topic,
                **extra: Any,
            ) -> None:
                key = object_id + ("_" + suffix if suffix else "")
                payload = {
                    "name": name,
                    "unique_id": f"{self.gateway_id}_{key}",
                    "device": device,
                    "state_topic": state_topic,
                    "value_template": _template(field, domain == "binary_sensor"),
                    "json_attributes_topic": state_topic,
                    "json_attributes_template": "{{ value_json.attributes | tojson }}",
                    "availability": [
                        {"topic": f"{self.topic_prefix}/availability"},
                        {"topic": f"{self.topic_prefix}/panel_availability"},
                    ],
                    "availability_mode": "all",
                    "qos": 1,
                    **extra,
                }
                if domain == "binary_sensor":
                    payload.update(payload_on="ON", payload_off="OFF")
                topic = f"{self.discovery_prefix}/{domain}/{self.gateway_id}/{key}/config"
                result[topic] = _json(payload)

            if state.kind == "area":
                # Reserved inert placeholder required by HA. Future commands require a separate
                # security design: this gateway never subscribes or dispatches panel commands.
                add(
                    "alarm_control_panel",
                    command_topic=f"{self.topic_prefix}/command/area/{_token(state.id)}",
                    supported_features=[],
                    code_arm_required=False,
                )
                alarm_topic = (
                    f"{self.discovery_prefix}/alarm_control_panel/"
                    f"{self.gateway_id}/{object_id}/config"
                )
                alarm_config = json.loads(result[alarm_topic])
                alarm_config["value_template"] = (
                    "{{ 'None' if value_json.state in [none, 'unknown'] else value_json.state }}"
                )
                result[alarm_topic] = _json(alarm_config)
                for mode in ("away", "home"):
                    add(
                        "binary_sensor",
                        f"ready_to_arm_{mode}",
                        f"Area ready to arm {mode}",
                        f"attributes.ready_{mode}",
                        entity_category="diagnostic",
                    )
                for kind in ("burglary", "gas", "fire"):
                    add(
                        "sensor",
                        f"alarms_{kind}",
                        f"{kind.title()} alarm issues",
                        f"attributes.alarms_{kind}",
                        device_class="enum",
                        options=["no_issues", "supervisory", "trouble", "alarm"],
                    )
                add(
                    "sensor",
                    "faulting_points",
                    "Faulting points",
                    "attributes.faulting_points",
                    unit_of_measurement="points",
                )
            elif state.kind == "door":
                for key, name in (
                    ("locked", "Locked"),
                    ("secured", "Secured"),
                    ("cycling", "Momentarily unlocked"),
                ):
                    # No lock device_class: HA uses ON=unlocked, opposite the native boolean.
                    add("binary_sensor", key, name, f"attributes.{key}")
            elif state.kind == "fault":
                name, device_class, enabled = FAULTS.get(state.id, (state.name, "problem", False))
                extra: dict[str, Any] = {
                    "entity_category": "diagnostic",
                    "enabled_by_default": enabled,
                }
                if device_class:
                    extra["device_class"] = device_class
                add("binary_sensor", name=name, **extra)
            else:
                add("binary_sensor")
        if states and not any(state.kind == "fault" for state in states):
            key = f"{_token(info.serial)}_connection"
            result[f"{self.discovery_prefix}/binary_sensor/{self.gateway_id}/{key}/config"] = _json(
                {
                    "name": "Panel connection",
                    "unique_id": f"{self.gateway_id}_{key}",
                    "device": panel_device,
                    "device_class": "connectivity",
                    "entity_category": "diagnostic",
                    "qos": 1,
                    "state_topic": f"{self.topic_prefix}/panel_availability",
                    "payload_on": "online",
                    "payload_off": "offline",
                    "availability_topic": f"{self.topic_prefix}/availability",
                }
            )
        return result
