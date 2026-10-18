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


def test_plaintext_config_preflight(tmp_path, capsys):
    path = write_config(tmp_path)
    path.write_text(
        path.read_text().replace("[mqtt]\n", '[mqtt]\ntransport = "plaintext"\nport = 1883\n')
    )
    with (
        patch.object(sys, "argv", ["bosch-mqtt-gateway", "--config", str(path), "--check-config"]),
        patch("bosch_mqtt_gateway.mqtt.ssl.create_default_context") as create,
    ):
        main()
    create.assert_not_called()
    assert capsys.readouterr().out == "Configuration and secret files are valid\n"


@pytest.mark.parametrize(
    "old,new,expected",
    [
        (
            "[mqtt]\n",
            '[mqtt]\ntransport = "plaintext"\nca_file = "SECRET-CA-PATH"\n',
            "Configuration error: mqtt: plaintext transport cannot include ca_file, cert_file "
            "or key_file; remove them or select tls",
        ),
        (
            'certificate_sha256 = "' + "ab" * 32 + '"',
            'certificate_sha256 = "SECRET-PIN"',
            "Configuration error: panel.certificate_sha256: certificate_sha256 must be exactly "
            "64 hexadecimal characters",
        ),
        (
            'username = "gateway"\n',
            "",
            "Configuration error: mqtt.username: required field is missing",
        ),
        (
            "[mqtt]\n",
            '[mqtt]\n"SECRET\\n\\u001bKEY" = "SECRET-VALUE"\n',
            "Configuration error: mqtt.(unknown field): unrecognized field; remove it or use a "
            "documented configuration field",
        ),
        (
            "[mqtt]\n",
            '[mqtt]\nport = "SECRET-WRONG-TYPE"\n',
            "Configuration error: mqtt.port: must be an integer",
        ),
        (
            "[gateway]\n",
            '"SECRET\\nROOT" = "SECRET-VALUE"\n[gateway]\n',
            "Configuration error: (unknown field): unrecognized field; remove it or use a "
            "documented configuration field",
        ),
    ],
)
def test_safe_actionable_schema_errors(tmp_path, capsys, old, new, expected):
    path = write_config(tmp_path)
    path.write_text(path.read_text().replace(old, new))
    with patch.object(sys, "argv", ["gateway", "--config", str(path), "--check-config"]):
        with pytest.raises(SystemExit) as error:
            main()
    assert error.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == expected + "\n"
    assert "SECRET" not in output.err
    assert "\x1b" not in output.err


@pytest.mark.parametrize(
    "change,expected",
    [
        ("secret", "mqtt.password_file (FileNotFoundError)"),
        ("ca", "mqtt TLS (FileNotFoundError)"),
    ],
)
def test_file_errors_identify_operation_without_path(tmp_path, capsys, change, expected):
    path = write_config(tmp_path)
    if change == "secret":
        path.write_text(path.read_text().replace(f"{tmp_path}/mqtt", f"{tmp_path}/SECRET-missing"))
    else:
        path.write_text(
            path.read_text().replace("[mqtt]\n", f'[mqtt]\nca_file = "{tmp_path}/SECRET-CA"\n')
        )
    with patch.object(sys, "argv", ["gateway", "--config", str(path), "--check-config"]):
        with pytest.raises(SystemExit) as error:
            main()
    assert error.value.code == 2
    output = capsys.readouterr().err
    assert expected in output
    assert "SECRET" not in output
    assert str(tmp_path) not in output
