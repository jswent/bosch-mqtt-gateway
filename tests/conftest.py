from pathlib import Path

import pytest

from bosch_mqtt_gateway.config import Config


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config.model_validate(
        {
            "gateway": {
                "id": "test",
                "topic_prefix": "bosch/test",
                "state_dir": str(tmp_path),
                "reconnect_min": 0.1,
                "reconnect_max": 1.0,
            },
            "panel": {"host": "panel", "automation_code_file": "/not-read", "ca_file": "/not-read"},
            "mqtt": {"host": "mqtt", "username": "test", "password_file": "/not-read"},
        }
    )
