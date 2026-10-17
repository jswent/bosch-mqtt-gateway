# Architecture and security boundary

Version 0.1 is one process per panel, using Python asyncio. FastAPI shares that
process for read-only operational endpoints. The Bosch library supplies panel
capability negotiation, entity enumeration, event decoding and state semantics.
The gateway replaces its unverified TLS connection, wire logging, frame reader,
automatic reconnection and unrestricted command transport at a narrow adapter
boundary. The upstream dependency is pinned and must be re-reviewed on upgrade.

## Trust boundaries

The panel and MQTT broker are independent authenticated TLS peers. Configure a
trusted panel CA plus hostname, or an independently verified SHA-256 fingerprint
of the panel's DER leaf certificate. Pin validation occurs before basic-info or
login traffic. A pin must be obtained through a trusted administrative channel;
automatically trusting a certificate observed on an untrusted network defeats this
protection. Broker TLS always validates its certificate chain and hostname, with
optional client certificates. There is no plaintext or skip-verification setting.

The adapter permits only reviewed numeric read, login and event-subscription
commands at the final Mode 2 send boundary. No actuator method is exposed by the
adapter. Login and subscription change protocol session state; neither may arm,
disarm, bypass a point, change an output or operate a door. Upstream history reads
are suppressed because no native entity requires them and they require broader
panel privileges. Use the minimum panel account permissions available.

MQTT is a publication-only capability. No subscription or inbound message handler
exists in the application. MQTT 5 clean sessions have zero session expiry, so an
old subscription cannot survive a prior client using the same client ID. Publish
paths are restricted in code to owned telemetry, availability and discovery
namespaces. Broker ACLs form a second independent boundary. Home Assistant must
use a different principal: it reads this gateway's state/discovery and cannot
publish into its state, availability or inert command namespace.

Home Assistant requires a command topic for an `alarm_control_panel`. The
explicitly inert topic in `discovery.py` is a compatibility declaration, not a
command capability. UI attempts do nothing, state is never optimistic, and the
broker ACL denies commands. Future write support requires a separate design:
explicit action scopes, actor authentication, authorization, audit events, replay
and stale-command protection, expiry and safe failure behavior. Do not implement
it by adding a wildcard subscription to this telemetry process.

## Telemetry lifecycle

Each MQTT session first publishes retained offline availability. A new panel
connection verifies identity, authenticates and reads a complete current
snapshot. Discovery and states are then retained with QoS 1. During this initial
publication, new observations queue behind the captured snapshot. Availability is
set online only after discovery, snapshot and queued observations are acknowledged.
A fresh snapshot is required after either transport reconnects.

Events take an entity-specific path; changing one point does not serialize all
600 points. A fixed number of FIFO publication workers shard events by entity
identity. That preserves order for each entity while allowing bounded MQTT
publications in parallel. Duplicate state payloads are suppressed within a
worker. Transitions are not intentionally coalesced; if a bounded queue overflows,
readiness fails, the session is torn down, and a fresh snapshot is obtained.
The overflow counter makes lost transitions visible. This gateway exposes current
state, not a durable alarm-event journal; no claim of lossless history is made
across outages or overflow.

The MQTT last will marks transport failure unavailable. Graceful stop and panel
failure explicitly revoke availability. Panel health includes periodic reads.
Detection has finite timeouts: a disconnected cable may remain apparently online
until the poll/request timeout or MQTT keepalive detects it. A broker outage also
makes the MQTT integration unavailable. Availability is an operational signal,
not a substitute for the alarm panel's own life-safety signaling.

A disk journal records the union of old and new retained topics before discovery
changes. Removed entities receive retained empty messages (tombstones), and only
then is the journal compacted. Atomic replace plus fsync protects against partial
writes. Corruption, foreign topics or a namespace mismatch fails closed. Keep the
state directory across upgrades. Changing gateway ID or namespace requires
retiring the old retained discovery with its old configuration first. A file
lock prevents two processes sharing a state directory; deployment must also
ensure gateway IDs and topic namespaces are unique across hosts.

## Diagnostics and logging

`GET /healthz` is process liveness; `GET /readyz` is telemetry readiness. Both expose
minimal unauthenticated booleans. `/status`, `/metrics` and `/debug/state` require a
constant-time checked bearer token with at least 32 characters, configured through
a secret file. These endpoints are disabled without a token. Debug state includes
security-sensitive occupancy/alarm information: keep HTTP on loopback and use an
SSH tunnel. Non-loopback bind requires authentication and should only be used on
a private container network or behind a properly authenticated TLS proxy.

There are no mutation endpoints, CORS configuration, interactive API docs or
trusted forwarding headers. Access logs are disabled to avoid request data
leakage. Structured JSON logs support DEBUG/INFO/WARNING/ERROR; upstream wire
logs are suppressed even at DEBUG. Registered secrets are redacted; exception
payloads and raw frames are never logged. Audit lifecycle messages use a separate
logger name. Future action auditing belongs behind the future authorization
boundary, not in generic debug logging.

Metrics are bounded in memory and include event counts, reconnects, queue
overflows, failures and callback-to-broker-ack latency percentiles over the latest
4096 publications. They do **not** measure Home Assistant processing or browser
rendering. The development lifecycle harness separately measures delivery to
Home Assistant's `state_changed` WebSocket stream.

## Deployment scope

The recommended production unit is a dedicated Linux VM with a hardened systemd
service. Keep panel credentials out of Home Assistant entirely. Restrict network
paths so Home Assistant can reach the broker but not the panel; only the gateway
VM can reach the panel's Mode 2 port. Broker authentication and ACL policy are
required configuration on the user's existing dedicated broker. The local Compose
stack uses entirely separate generated certificates, credentials, simulator and
Home Assistant storage. Nothing in development targets the real panel or broker.

This implementation strengthens the specific trust and authorization boundaries
identified above; it does not assert that every native Bosch integration is
universally insecure. Physical B9512G firmware behavior and deployment firewall
policy still require validation before switching production consumers.
