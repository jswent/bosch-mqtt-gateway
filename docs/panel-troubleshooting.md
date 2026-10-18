# Diagnosing panel startup

A running HTTP server and a successful configuration check do not mean telemetry
is ready. Discovery is published only after the panel identity, inventory and
initial states have been read. Look for `telemetry_ready` and an HTTP 200 response
from `/readyz` to confirm synchronization.

`session_failed phase=connecting_panel error_type=TimeoutError` means the panel
startup deadline expired. MQTT was connected before this phase; the message does
not establish that the panel TCP connection, TLS handshake or login succeeded.
The default `[panel].timeout = 60.0` covers the **entire** startup sequence.

At INFO level, `panel_startup_failed` identifies the stage and elapsed time:

| Stage | Operation to investigate |
| --- | --- |
| `connecting_tls` | DNS, TCP routing/firewalls and TLS handshake/CA validation |
| `verifying_certificate` | Configured certificate fingerprint versus the TLS peer |
| `reading_identity` | Initial Mode 2 identity/capability response |
| `authenticating` | Mode 2 automation credential and panel session availability |
| `loading_inventory` | Extended identity, configured entities and their names |
| `reading_status` | Initial area, point, output, door and fault states |
| `subscribing` | Panel event subscription acknowledgement |

DEBUG level additionally records `panel_startup_stage` transitions with elapsed
time. Startup logs exclude credentials, certificate contents, entity names, wire
payloads and raw exception messages. Upstream wire logging remains disabled.

## Authentication rejection

`PermissionError` during `authenticating` can mean either a rejected automation
passcode or that the panel has no free automation sessions. The gateway records a
separate `panel_authentication_rejected` line with a fixed reason:

- `not_authorized`: the panel returned a denial or an empty authentication result.
  Privately compare the configured `panel_code` with the panel's Automation
  Passcode in RPS under Automation / Remote App. For B/G panels this is the
  automation credential; a separate keypad user code is not used by the library.
  Preserve case and check for accidentally included spaces or literal quotes.
- `max_connections`: the panel reported its connection limit. Check for the old
  native Bosch integration or another Mode 2 client holding a session; release
  the unneeded client before retrying. This response does not identify the client
  or establish that the supplied passcode is correct.
- `unexpected_response`: the authentication result is not one of the supported
  outcomes. Report the fixed reason and startup stage without enabling wire logs.

The reason labels come from the authentication response; no credentials or raw
response bytes are logged. A file's byte count alone cannot establish whether its
contents are the correct automation passcode. The native integration's
[authentication documentation](https://www.home-assistant.io/integrations/bosch_alarm/#authentication)
explains which credential each panel family uses.

After correcting `/etc/bosch-mqtt-gateway/secrets/panel_code`, restart the service:
systemd copies source credentials into the service's private credential directory
at startup. Do not edit `/run/credentials` or paste a passcode into diagnostics.

## Check the VM network path

Run these from the Debian gateway VM, replacing `PANEL_IP` with the configured
panel address and adjusting port 7700 if necessary:

```sh
ip route get PANEL_IP
timeout 10 openssl s_client -connect PANEL_IP:7700 -brief </dev/null
```

These checks send no Mode 2 credentials or alarm commands. `s_client` is only a
connectivity/TLS probe: it does not verify the gateway's configured certificate
pin. A self-signed verification warning is expected for a self-signed certificate;
look for an established TLS connection. Do not adopt a new fingerprint from this
probe without independently verifying it.

An isolated Proxmox bridge between HAOS and the gateway only provides the broker
path. The gateway also needs a route to the panel, with outbound TCP to the panel
port allowed through the VM, Proxmox and intervening network firewalls. If the
shell probe succeeds but the service remains at `connecting_tls`, compare the
configured host/port and any service `IPAddressAllow`/`IPAddressDeny` restrictions.
Check for another active Mode 2 client when investigating session availability.

If startup reaches `loading_inventory`, a large panel using per-entity name reads
can exhaust the total budget despite responding to requests. Only after checking
that stage, a temporary `[panel].timeout = 180.0` can help distinguish slow
initialization from a stalled read. This also lengthens per-command timeouts, so
review the final value after commissioning; it does not change the event poll
interval. A longer timeout does not repair an unreachable panel.

After updating a noneditable production installation, reinstall the package as
shown in the [deployment guide](deployment.md#updating-the-installed-application), then restart the service.
Capture the new `panel_startup_failed` line and the adjacent `session_failed`
line. Preserve configuration and independently verified panel trust while
diagnosing connectivity.
