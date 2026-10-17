from pathlib import Path

import pytest
from pydantic import ValidationError

from bosch_mqtt_gateway.config import (
    GatewayConfig,
    HTTPConfig,
    MQTTConfig,
    PanelConfig,
    read_secret,
)


@pytest.mark.parametrize(
    "prefix", ["a/#", "a/+", "/a", "a/", "a//b", "$SYS/a", "a\x00b", "a b", "é"]
)
def test_reject_unsafe_topics(prefix):
    with pytest.raises(ValidationError):
        GatewayConfig(topic_prefix=prefix)


@pytest.mark.parametrize("name", ["../escape", "+", "#", "", "a/b", "a b", "é", "a" * 65])
def test_reject_unsafe_id(name):
    with pytest.raises(ValidationError):
        GatewayConfig(id=name)


def test_fail_closed_unknown_config():
    with pytest.raises(ValidationError):
        GatewayConfig(commands_enabled=True)
    with pytest.raises(ValidationError):
        PanelConfig(host="panel", automation_code_file="secret", insecure=True)


def test_panel_requires_trust():
    with pytest.raises(ValidationError):
        PanelConfig(host="panel", automation_code_file="secret")
    config = PanelConfig(host="panel", automation_code_file="secret", certificate_sha256="AB" * 32)
    assert config.certificate_sha256 == "ab" * 32


@pytest.mark.parametrize("pin", ["", "aa", "z" * 64, "aa:" * 32])
def test_invalid_pin(pin):
    with pytest.raises(ValidationError):
        PanelConfig(host="panel", automation_code_file="secret", certificate_sha256=pin)


def test_remote_api_requires_token():
    with pytest.raises(ValidationError):
        HTTPConfig(host="0.0.0.0")
    assert HTTPConfig(host="::1").token_file is None
    assert HTTPConfig(host="0.0.0.0", token_file="/secret").token_file == "/secret"


def test_secret_validation(tmp_path: Path):
    path = tmp_path / "secret"
    path.write_text("sensitive-value\n")
    assert read_secret(str(path)) == "sensitive-value"
    with pytest.raises(ValueError):
        read_secret(str(path), minimum=32)
    path.write_text("a" * 4097)
    with pytest.raises(ValueError, match="too large"):
        read_secret(str(path))
    path.write_text("a\nb")
    with pytest.raises(ValueError):
        read_secret(str(path))


def test_mqtt_client_tls_pair_required():
    with pytest.raises(ValidationError):
        MQTTConfig(host="broker", username="gateway", password_file="secret", cert_file="cert")


def test_mqtt_plaintext_and_inline_password_forbidden():
    with pytest.raises(ValidationError):
        MQTTConfig(host="broker", username="gateway", password="secret", tls=False)


def test_limits_and_namespace_overlap():
    with pytest.raises(ValidationError):
        GatewayConfig(queue_capacity=599)
    with pytest.raises(ValidationError):
        GatewayConfig(topic_prefix="homeassistant/foo")
    with pytest.raises(ValidationError):
        GatewayConfig(reconnect_min=10.0, reconnect_max=1.0)
