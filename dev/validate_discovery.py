"""Validate representative discovery against the running HA image, without server changes."""

import json
import shutil
import subprocess
from pathlib import Path

from bosch_mqtt_gateway.discovery import FAULTS, DiscoveryBuilder
from bosch_mqtt_gateway.models import EntityState, PanelInfo

VALIDATE = """
import asyncio
import json
import sys
from collections import Counter
from homeassistant.components.mqtt import alarm_control_panel, binary_sensor, sensor
from homeassistant.core import HomeAssistant

async def main():
    # Separate process and in-memory context; never starts HA or touches server config.
    hass = HomeAssistant('/tmp/bosch-schema-validation-unused')
    schemas = {
        'alarm_control_panel': alarm_control_panel.DISCOVERY_SCHEMA,
        'binary_sensor': binary_sensor.DISCOVERY_SCHEMA,
        'sensor': sensor.DISCOVERY_SCHEMA,
    }
    counts = Counter()
    for topic, payload in json.load(sys.stdin).items():
        domain = topic.split('/')[1]
        config = json.loads(payload)
        validated = schemas[domain](config)
        removed = set(config) - set(validated)
        if removed:
            raise RuntimeError(f'{topic}: silently removed fields {removed}')
        counts[domain] += 1
    expected = {'binary_sensor': 631, 'alarm_control_panel': 4, 'sensor': 16}
    if dict(counts) != expected:
        raise RuntimeError(f'Unexpected entity counts: {dict(counts)}')
    print(json.dumps(dict(counts), sort_keys=True))

asyncio.run(main())
"""


def fixtures() -> dict[str, str]:
    states = [EntityState("point", str(i), f"Point {i}", bool(i % 2)) for i in range(1, 601)]
    states.extend(
        EntityState(
            "area",
            str(i),
            f"Area {i}",
            "disarmed",
            {
                "ready_away": True,
                "ready_home": None,
                "faulting_points": 0,
                "alarms_burglary": "no_issues",
                "alarms_gas": "no_issues",
                "alarms_fire": "no_issues",
            },
        )
        for i in range(1, 5)
    )
    states.extend(EntityState("output", str(i), f"Output {i}", False) for i in range(1, 9))
    states.extend(EntityState("fault", key, name, False) for key, (name, _, _) in FAULTS.items())
    states.append(
        EntityState(
            "door", "1", "Door", "locked", {"locked": True, "secured": False, "cycling": False}
        )
    )
    return DiscoveryBuilder(
        gateway_id="bosch_dev", topic_prefix="bosch/dev", discovery_prefix="homeassistant"
    ).build(PanelInfo("SIM600", "B9512G", "1.0"), tuple(states))


def main() -> None:
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("Docker is required for Home Assistant schema validation")
    result = subprocess.run(
        [docker, "compose", "exec", "-T", "homeassistant", "python", "-c", VALIDATE],
        input=json.dumps(fixtures()),
        text=True,
        capture_output=True,
        check=True,
        cwd=Path(__file__).resolve().parent.parent,
        timeout=60,
    )
    print(result.stdout.strip())


if __name__ == "__main__":
    main()
