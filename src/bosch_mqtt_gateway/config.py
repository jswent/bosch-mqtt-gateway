"""Strict, file-based configuration. Credentials never belong in TOML or command arguments."""

from __future__ import annotations

import ipaddress
import re
import tomllib
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Port = Annotated[int, Field(ge=1, le=65535)]


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class GatewayConfig(StrictConfig):
    id: str = "bosch"
    topic_prefix: str = "bosch/panel"
    discovery_prefix: str = "homeassistant"
    state_dir: str = "/var/lib/bosch-mqtt-gateway"
    queue_capacity: Annotated[int, Field(ge=600, le=65536)] = 8192
    publish_workers: Annotated[int, Field(ge=1, le=64)] = 16
    reconnect_min: Annotated[float, Field(ge=0.1, le=60)] = 1.0
    reconnect_max: Annotated[float, Field(ge=1, le=300)] = 30.0
    heartbeat_interval: Annotated[float, Field(ge=1, le=60)] = 5.0

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value):
            raise ValueError("gateway.id must contain 1-64 ASCII letters, digits, _ or -")
        return value

    @field_validator("topic_prefix", "discovery_prefix")
    @classmethod
    def valid_topic(cls, value: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", value) or len(value) > 128:
            raise ValueError("topic prefix must contain safe nonempty MQTT path segments")
        return value

    @model_validator(mode="after")
    def check_backoff(self) -> Self:
        if self.reconnect_min > self.reconnect_max:
            raise ValueError("reconnect_min cannot exceed reconnect_max")
        if (
            self.topic_prefix == self.discovery_prefix
            or self.topic_prefix.startswith(self.discovery_prefix + "/")
            or self.discovery_prefix.startswith(self.topic_prefix + "/")
        ):
            raise ValueError("telemetry and discovery namespaces must be separate")
        return self


class PanelConfig(StrictConfig):
    host: str
    port: Port = 7700
    automation_code_file: str
    user_code_file: str | None = None
    ca_file: str | None = None
    server_name: str | None = None
    certificate_sha256: str | None = None
    timeout: Annotated[float, Field(ge=1, le=300)] = 60.0

    @field_validator("certificate_sha256")
    @classmethod
    def valid_pin(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError("certificate_sha256 must be exactly 64 hexadecimal characters")
        return value.lower() if value else value

    @model_validator(mode="after")
    def require_identity(self) -> Self:
        if not self.ca_file and not self.certificate_sha256:
            raise ValueError(
                "panel requires ca_file or an independently verified certificate_sha256"
            )
        return self


class MQTTConfig(StrictConfig):
    host: str
    transport: Literal["tls", "plaintext"] = "tls"
    port: Port = 8883
    username: str
    password_file: str
    ca_file: str | None = None
    cert_file: str | None = None
    key_file: str | None = None
    keepalive: Annotated[int, Field(ge=5, le=120)] = 15
    timeout: Annotated[float, Field(ge=1, le=60)] = 10.0

    @model_validator(mode="after")
    def client_certificate_pair(self) -> Self:
        if self.transport == "plaintext" and any(
            value is not None for value in (self.ca_file, self.cert_file, self.key_file)
        ):
            raise ValueError("mqtt plaintext transport cannot use TLS certificate settings")
        if bool(self.cert_file) != bool(self.key_file):
            raise ValueError("mqtt cert_file and key_file must be configured together")
        return self


class HTTPConfig(StrictConfig):
    host: str = "127.0.0.1"
    port: Port = 8080
    token_file: str | None = None

    @model_validator(mode="after")
    def require_remote_auth(self) -> Self:
        try:
            loopback = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            loopback = self.host == "localhost"
        if not loopback and not self.token_file:
            raise ValueError("non-loopback HTTP listeners require token_file")
        return self


class LoggingConfig(StrictConfig):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "text"] = "json"


class Config(StrictConfig):
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    panel: PanelConfig
    mqtt: MQTTConfig
    http: HTTPConfig = Field(default_factory=HTTPConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_config(path: Path) -> Config:
    with path.open("rb") as stream:
        return Config.model_validate(tomllib.load(stream))


def read_secret(path: str, *, minimum: int = 1) -> str:
    """Read a bounded secret file without putting its value into diagnostics."""
    with Path(path).open("rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("secret file is too large")
    value = raw.decode("utf-8").rstrip("\r\n")
    if len(value) < minimum or any(c in value for c in "\x00\r\n"):
        raise ValueError("secret file has invalid contents")
    return value
