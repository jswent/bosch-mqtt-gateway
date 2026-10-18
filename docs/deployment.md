# Deployment and development lifecycle

Production: use a dedicated small Linux VM (Python 3.12–3.14), a separate unprivileged service account, and the supplied systemd unit. Keep this VM outside the general client network. No simulator or development control API belongs in production.

1. Create `bosch-gateway` as a system account with no login/home. Install the reviewed source at `/opt/bosch-mqtt-gateway`; use `uv sync --frozen --no-dev --no-editable --python /usr/bin/python3 --no-managed-python` from the committed lock on Debian 13. This keeps the interpreter outside home directories hidden by `ProtectHome=yes`. Root owns source and virtualenv; the service may not modify them.
2. Set `/etc/bosch-mqtt-gateway` to root:bosch-gateway (0750), its config to root:bosch-gateway (0640), and its `secrets` directory to root:root (0700). Put panel automation code, MQTT password, and a random debug bearer token in separate root-only 0600 files. The unit's `LoadCredential` gives the service private copies. In config, use `/run/credentials/bosch-mqtt-gateway.service/panel_code`, `mqtt_password`, and `debug_token`. Never embed secret values in TOML, unit arguments, environment files, or shell history.
3. Install independently verified panel trust and, when using MQTT TLS, broker CA certificates as read-only files. Match panel `server_name` to its identity. For self-signed legacy panels, independently obtain and verify its SHA256 certificate fingerprint before configuring the pin. Never learn trust automatically from the first network connection. Rotate trust deliberately before certificate replacement.
4. Configure broker TLS on 8883 with a trusted certificate and unique per-gateway password, or use the explicit isolated-network plaintext option below. For standalone Mosquitto use `deploy/mosquitto.conf` for TLS/persistence; for the HAOS Mosquitto app use its supported configuration/customization instead. Adapt `deploy/mosquitto.acl`; gateway gets write access only to its state/availability and discovery paths, no read access. HA reads those topics and may write its birth status only. Neither principal may publish to the inert alarm `command` path. Avoid other accounts with broader overlapping grants. Verify negative PUBACKs and failed subscriptions in staging.
5. Set HTTP to `127.0.0.1`; access diagnostics through SSH forwarding. No public reverse proxy is required. The authenticated API is GET-only. Restrict SSH to management networks.
6. At the VM/hypervisor firewall permit outbound TCP only to the exact panel IP:7700 and broker IP and selected port (8883 for TLS or explicitly configured 1883 for plaintext), plus explicitly needed DNS/NTP/update infrastructure. Deny panel access from HA and other general hosts. On the panel network permit this VM only. Apply stateful reply rules; do not depend only on application permissions. Add systemd `IPAddressDeny=any` / narrow `IPAddressAllow=` entries if your distribution supports cgroup BPF, covering loopback, panel, broker and DNS as needed.
7. Copy `deploy/config.example.toml` to the configuration path and replace reserved examples. Install `deploy/bosch-mqtt-gateway.service` (its ExecStartPre validates configuration and credentials), run `systemd-analyze verify`, enable/start, inspect journal and `/readyz`. Check 600 point discovery and panel-loss unavailability before connecting production. Maintain VM security updates, log rotation, and encrypted backup of configuration and identity manifest. Logs must not contain credentials.

The upstream protocol does not itself guarantee a read-only panel credential. The gateway enforces a command allowlist, broker ACLs remove the MQTT command route, and network isolation limits consequences. Configure the least panel authority supported by the actual firmware. Test only in a maintenance window before production cutover.

## Plaintext MQTT on an isolated Proxmox bridge

For the dedicated gateway-to-HAOS internal bridge, plaintext is an explicit opt-in:

```toml
[mqtt]
transport = "plaintext"
host = "192.0.2.20" # Replace with HAOS's static address on the internal bridge.
port = 1883
username = "gateway" # Must match the dedicated broker account and its ACL.
password_file = "/run/credentials/bosch-mqtt-gateway.service/mqtt_password"
keepalive = 15
```

Remove `ca_file`, `cert_file`, and `key_file` from this MQTT section. They are
rejected in plaintext mode rather than silently ignored. Omitting `transport`
keeps verified TLS, even if the configured port is 1883; connection failures never
trigger a plaintext retry. The panel connection still requires verified TLS or an
independently verified certificate pin.

Attach both guests to a bridge without physical uplinks, assign static addresses
in a separate subnet, and leave default gateways off these internal interfaces.
Keep each guest's existing management route; the gateway has a separately
restricted path to the panel. Disable forwarding through the gateway VM and allow
only its internal address to reach HAOS port 1883 on this path. A bridge is not a
firewall: adding another guest changes who can access the segment. HAOS's app may
also expose the port on other interfaces; preserve necessary existing clients
while restricting exposure with the appropriate VM/network firewall.

Plaintext sends MQTT credentials and alarm telemetry without encryption or
cryptographic broker authentication. Use it only when the hypervisor and attached
guests are trusted. Dedicated MQTT credentials, publication-only gateway ACLs,
read-only HA permissions for Bosch topics, and inert alarm commands still apply.
The application logs the selected transport at startup. The default Compose lab
continues to use TLS; `dev/probe_plaintext.py` starts a separate disposable local
broker to validate the opt-in without changing that stack.

## Updating the installed application

A production non-editable installation needs to be refreshed after pulling source
changes. From the installed repository, synchronize the locked environment and
explicitly reinstall the gateway package so a cached wheel cannot leave the old
code running:

```sh
git pull --ff-only
uv sync --frozen --no-dev --no-editable --python /usr/bin/python3 \
  --no-managed-python --reinstall-package bosch-mqtt-gateway
```

Run installation with the same root ownership as the initial deployment. In the
Debian walkthrough, the `uv` executable is `/opt/uv/bin/uv`. Restart the service and
check readiness after updating. Preserve configuration, credentials and
`/var/lib/bosch-mqtt-gateway` across upgrades.

## Diagnosing startup

`243/CREDENTIALS` comes from systemd before the application starts. Check the
unit's `LoadCredential` source paths and the existence/type/ownership of all three
files. Root-only `0600` source files are intentional; do not make them readable by
other users to troubleshoot this failure. A temporary `systemd-run` credential
probe can distinguish credential setup from application validation without
printing secret contents.

Application validation failures name a schema field or section and a controlled
explanation. Values, arbitrary unknown field names and raw validator messages are
never printed. For example, plaintext MQTT cannot retain `ca_file`, `cert_file` or
`key_file`; panel trust is still mandatory; a certificate pin is 64 hexadecimal
characters without colons. File-access and secret errors identify the operation
using fixed field labels, without printing paths or file contents. The debug token
requires at least 32 characters.

The unit runs `--check-config` automatically with its credentials loaded. A normal
shell invocation cannot access the unit's private runtime credential directory.
Successful preflight checks local configuration and files; `/readyz` becomes true
only after the live panel and MQTT synchronization completes.

## Local development

Requires Docker Compose, `uv`, and OpenSSL-compatible TLS. Run `uv sync --frozen`, `.venv/bin/python dev/bootstrap.py`, then `docker compose up -d --build` and `.venv/bin/python dev/ha_bootstrap.py`. The bootstrap generates disposable credentials and a 30-day CA under gitignored `dev/generated`. It creates a real HA owner through onboarding and configures MQTT through the config-flow/certificate-upload APIs; it never fabricates `.storage` entries. Credentials and HA tokens are for this lab only.

All host listeners bind loopback: HA 18123, debug 18080, simulator test control 18081, simulator TLS 17700, broker TLS 18883. The dedicated Compose bridge permits outbound internet for HA setup; all host listeners remain loopback. Enforce production egress restrictions at the VM firewall. HA may report device-discovery warnings because container capabilities are dropped. The simulator implements actual TLS Mode 2 framing with 600 synthetic points, four areas, eight outputs, authentication, state reads, subscriptions, and deterministic point updates. All action opcodes are denied. It never connects to physical devices.

Run `dev/check-lifecycle.sh` for bootstrap, build, real HA setup, and E2E/performance checks. Reports go in gitignored `dev/generated`. `docker compose down` stops services and preserves HA; `docker compose down -v` destroys only this lab's named HA volume. To fully reset, stop and remove the volume and explicitly delete `dev/generated`. Rerun bootstrap. Host generated directory is 0700; read-only container mounts require readable files across Docker Desktop UIDs. This is disposable lab handling, not production secret handling.

Performance records simulator emission timestamp and HA `state_changed` timestamp over an authenticated websocket; same Docker VM clock is used by both endpoints. Client receive latency is reported separately. This measures the complete simulator → TLS library → gateway → broker → real HA pipeline, not just Python callback speed. Results describe this machine and load only.

## Observed validation (2026-10-17)

The Docker lab passed discovery of all 600 point entities and four actual alarm control panel entities, denied command QoS1 publications for both principals, and denied delivery to the gateway's test subscription. An actual HA `alarm_disarm` service call returned HTTP 200 without any panel action opcode or alarm-state change. Simulated disconnect, malformed frame, graceful shutdown, SIGKILL/Last Will, broker restart, and HA restart all recovered. The final 100 sequential event samples measured simulator-emission to HA-state p50 1.1465 ms, p95 1.4548 ms, max 2.0677 ms; this is not a throughput benchmark. The separate `dev/benchmark.py` records burst and sustained measurements.

`dev/probe_puback.py` also verified a real broker negative PUBACK raises `PublishRejected` and session exit sends MQTT 5 Disconnect-with-Will: both the live observer and a fresh subscriber saw offline, proving retained unavailability. Results are written under `dev/generated`; no credentials are printed. Two simulator tests passed, including upstream library discovery of 600 points and a real TLS wire update of point 600.
