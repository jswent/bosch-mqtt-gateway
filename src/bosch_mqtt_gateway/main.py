"""One process per panel: Uvicorn and the telemetry supervisor share an asyncio loop."""

from __future__ import annotations

import argparse
import logging
import ssl
import sys
from pathlib import Path

import uvicorn
from pydantic import ValidationError

from .api import create_app
from .config import load_config, read_secret, safe_validation_errors
from .mqtt import tls_context
from .observability import configure_logging
from .runtime import Gateway, Secrets
from .storage import instance_lock


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish-only Bosch Mode 2 MQTT gateway")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate configuration and secret files, then exit",
    )
    args = parser.parse_args()
    operation = "configuration"
    try:
        config = load_config(args.config)
        # Check trust stores and client-key readability before starting any network tasks.
        operation = "mqtt TLS"
        tls_context(config.mqtt)
        if config.panel.ca_file:
            operation = "panel.ca_file"
            ssl.create_default_context(cafile=config.panel.ca_file)
        operation = "panel.automation_code_file"
        panel_code = read_secret(config.panel.automation_code_file)
        operation = "panel.user_code_file"
        user_code = (
            read_secret(config.panel.user_code_file) if config.panel.user_code_file else None
        )
        operation = "mqtt.password_file"
        mqtt_password = read_secret(config.mqtt.password_file)
        operation = "http.token_file"
        debug_token = (
            read_secret(config.http.token_file, minimum=32) if config.http.token_file else None
        )
        values = Secrets(
            panel_code=panel_code,
            user_code=user_code,
            mqtt_password=mqtt_password,
            debug_token=debug_token,
        )
        operation = "logging"
        configure_logging(
            config.logging.level,
            config.logging.format,
            tuple(
                value
                for value in (
                    values.panel_code,
                    values.user_code,
                    values.mqtt_password,
                    values.debug_token,
                )
                if value
            ),
        )
        if args.check_config:
            print("Configuration and secret files are valid")
            return
        operation = "gateway.state_dir"
        with instance_lock(Path(config.gateway.state_dir)):
            operation = "running gateway"
            gateway = Gateway(config, values)
            logging.getLogger("bosch_mqtt_gateway.audit").info(
                "gateway_start commands_enabled=false mqtt_subscriptions=0 mqtt_transport=%s",
                config.mqtt.transport,
            )
            uvicorn.run(
                create_app(gateway),
                host=config.http.host,
                port=config.http.port,
                workers=1,
                access_log=False,
                log_config=None,
                proxy_headers=False,
                server_header=False,
                limit_concurrency=32,
                timeout_keep_alive=5,
            )
    except ValidationError as exc:
        for message in safe_validation_errors(exc):
            print(message, file=sys.stderr)
        raise SystemExit(2) from None
    except (ValueError, OSError, RuntimeError) as exc:
        # Parse/OS errors can echo file contents or sensitive configuration values.
        print(
            f"Gateway startup failed during {operation} ({type(exc).__name__}); "
            "check configuration and file access",
            file=sys.stderr,
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
