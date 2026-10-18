#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
uv sync --frozen
[ -f dev/generated/gateway.toml ] || .venv/bin/python dev/bootstrap.py
docker compose up -d --build
.venv/bin/python dev/ha_bootstrap.py
.venv/bin/python dev/validate_discovery.py
.venv/bin/python dev/e2e.py
.venv/bin/python dev/probe_puback.py
.venv/bin/python dev/probe_plaintext.py
.venv/bin/python dev/benchmark.py
