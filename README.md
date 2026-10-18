# Bosch MQTT Gateway

A read-only Bosch Mode 2 gateway for Home Assistant's native MQTT integration,
built on `bosch-alarm-mode2==0.4.11`. Intended for a dedicated Linux VM, separate
from Home Assistant and the panel network's other clients.

## What v1 does

- Publishes retained MQTT discovery, current states and availability using a
  publication-only broker identity. Verified TLS is the default; explicit plaintext
  MQTT is supported on a trusted isolated network. It never subscribes to MQTT.
- Preserves native Bosch panel/area/point/door/output device grouping and alarm,
  readiness, issue, fault and point states. Outputs and door controls become
  read-only binary sensors; see the [parity matrix](docs/home-assistant.md).
- Creates real `alarm_control_panel` entities. Their required **inert command
  topic** is declared and commented in `src/bosch_mqtt_gateway/discovery.py`.
  No command consumer exists, the supplied broker ACL rejects commands, and
  displayed state comes exclusively from the panel. Arming features are hidden;
  a disarm action may still appear in HA and has no effect.
- Verifies panel TLS identity before sending credentials, restricts outbound
  Mode 2 opcodes to reads/session setup, and suppresses upstream wire logs.
- Uses bounded queues and concurrent MQTT publications with FIFO order per
  entity. Reconnect requires a fresh snapshot. Overload fails availability and
  increments a visible counter instead of silently pretending state is current.
- Provides a minimal FastAPI health/debug service with no mutation routes,
  protected diagnostics, JSON logs, and bounded latency/queue/reconnect metrics.

There is no switch that enables writes in v1. Adding scoped actions later requires
an explicit authorization design; the inert alarm topic is the documented future
extension point, not a partially implemented command channel.

## Local development and verification

Prerequisites: Docker Compose, `uv`, and Python 3.12–3.14 (`uv` can install Python).
All panel data and credentials in this stack are synthetic. It uses a real TLS
Mode 2 simulator, Mosquitto with ACLs, the gateway, and Home Assistant 2026.10.2.

```sh
uv sync --frozen
uv run --frozen pytest --cov=bosch_mqtt_gateway --cov-report=term-missing
uv run --frozen ruff check .
uv run --frozen mypy src/bosch_mqtt_gateway
sh dev/check-lifecycle.sh
```

The lifecycle script bootstraps disposable secrets, builds the stack, onboards HA
and configures MQTT through real HA APIs, then checks discovery, ACL enforcement,
inert alarm actions, transport failure, crash will, restart and retained recovery.
The benchmark verifies delivery of 600-point bursts and sustained transitions,
measuring panel-simulator emission to HA `state_changed` timing. Reports are
written under gitignored `dev/generated`. Run `.venv/bin/python dev/benchmark.py`
independently to repeat the load test on a running stack.

Local endpoints are bound only to loopback:

| Service | URL/port |
| --- | --- |
| Home Assistant | http://127.0.0.1:18123 |
| Gateway liveness/readiness | http://127.0.0.1:18080/healthz and /readyz |
| Protected diagnostics | /status, /metrics, /debug/state on port 18080 |
| Simulator test controls | http://127.0.0.1:18081 |
| MQTT TLS | localhost:18883 |
| Simulator Mode 2 TLS | localhost:17700 |

HA's lab owner is `developer`; its generated password is in
`dev/generated/ha_password`. The gateway bearer token is in
`dev/generated/debug_token`. These files are never committed. The simulator
control API belongs only in this disposable stack. `docker compose down` stops
it; `docker compose down -v` also destroys its lab storage.

## Production deployment

Use the [deployment guide](docs/deployment.md),
[example configuration](deploy/config.example.toml),
[hardened systemd unit](deploy/bosch-mqtt-gateway.service) and
[broker ACL](deploy/mosquitto.acl). Use your existing dedicated MQTT broker with
separate gateway and HA principals. Keep panel credentials out of HA. Restrict
network access so only the gateway VM can reach Mode 2. For an isolated Proxmox
bridge to HAOS, see the [explicit plaintext MQTT option](docs/deployment.md#plaintext-mqtt-on-an-isolated-proxmox-bridge).

Install the locked environment, configure independently verified panel trust,
and configure it before starting:

```sh
uv sync --frozen --no-dev
```

The production service obtains secrets through systemd credentials and runs
`--check-config` automatically before startup, when those credentials are available. HTTP defaults
to loopback; access protected diagnostics through an SSH tunnel. The persistent
state directory contains the discovery cleanup journal and must survive upgrades.
Do not run two instances with the same gateway ID or MQTT namespace.
For a panel startup timeout, see the [stage and network diagnostics](docs/panel-troubleshooting.md).

Read the [architecture and security boundary](docs/architecture.md) and
[Home Assistant mapping/migration notes](docs/home-assistant.md) before cutover.
The gateway has been exercised with the simulator and real HA; deployment-specific
TLS, panel firmware, physical B9512G behavior and network latency must still be
verified in a controlled commissioning test. Test results are in
[validation](docs/validation.md).
