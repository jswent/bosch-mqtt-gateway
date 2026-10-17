import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from bosch_mqtt_gateway.main import main


def write_config(directory: Path) -> Path:
    (directory / "panel").write_text("panel-credential")
    (directory / "mqtt").write_text("mqtt-credential")
    (directory / "token").write_text("d" * 48)
    config = directory / "config.toml"
    config.write_text(f'''[gateway]
state_dir = "{directory}"
[panel]
host = "panel"
automation_code_file = "{directory}/panel"
certificate_sha256 = "{"ab" * 32}"
[mqtt]
host = "mqtt"
username = "gateway"
password_file = "{directory}/mqtt"
[http]
token_file = "{directory}/token"
''')
    return config


def test_check_config(tmp_path, capsys):
    path = write_config(tmp_path)
    with patch.object(sys, "argv", ["bosch-mqtt-gateway", "--config", str(path), "--check-config"]):
        main()
    assert capsys.readouterr().out == "Configuration and secret files are valid\n"


def test_config_errors_do_not_echo_values(tmp_path, capsys):
    path = tmp_path / "bad.toml"
    path.write_text("SECRET INVALID TOML")
    with patch.object(sys, "argv", ["bosch-mqtt-gateway", "--config", str(path)]):
        with pytest.raises(SystemExit) as error:
            main()
    assert error.value.code == 2
    assert "SECRET" not in capsys.readouterr().err


def test_startup_single_worker_no_proxy_or_access_log(tmp_path):
    path = write_config(tmp_path)
    with (
        patch.object(sys, "argv", ["bosch-mqtt-gateway", "--config", str(path)]),
        patch("bosch_mqtt_gateway.main.uvicorn.run") as run,
    ):
        main()
    assert run.call_args.kwargs["workers"] == 1
    assert run.call_args.kwargs["access_log"] is False
    assert run.call_args.kwargs["proxy_headers"] is False
