# Validation record — 2026-10-17

Validated locally on macOS/ARM64 with an OrbStack Linux Docker VM, Mosquitto
2.0.22, a TLS Mode 2 simulator and real Home Assistant 2026.10.2. Base images are
pinned by digest in Compose/Dockerfiles. The native Bosch mapping was researched
against the local HA checkout at `b7a099a1f28` and bosch-alarm-mode2 0.4.11.

The complete lifecycle ran on application revision `7aac354`; the final
application revision `d7d27aa` additionally aligns aiomqtt's warning threshold
with configured parallelism. The final benchmark below ran on `d7d27aa`, image
`sha256:2b1eb503eb124d27de2cfeccd4ed5b81774c976e3aa430ce7f4affc91c614665`.
Documentation-only follow-up commits do not change that image.

## Automated checks

| Check | Result |
| --- | --- |
| Python 3.12.13 | 136 tests passed |
| Python 3.13.14 | 136 tests passed; 93% statement coverage |
| Python 3.14.5 | 136 tests passed |
| Ruff lint and formatting | Passed |
| Mypy application sources | Passed |
| Locked production dependency audit | No known vulnerabilities reported by pip-audit at validation time |
| Actual HA discovery schemas | All 651 representative configs accepted, no supplied keys silently discarded |
| Runtime point scale | 600 real HA point entities, four actual alarm entities |

The schema set includes 600 points, four areas and their derived entities,
eight outputs, twelve panel faults and one door's three read-only entities.
Tests cover certificate trust before protocol bytes, forbidden opcodes,
fragmented/malformed/truncated frames, complete response identity sets,
immutable observations, event-local updates, bounded refresh tasks, secret
redaction, topic ownership, queue overflow, ordered transitions, snapshot races,
crash-safe journals, stale discovery removal, HTTP authentication and lack of
mutation routes. The test suite includes an actual loopback TLS wire exchange
with the existing Bosch library and all 600 points.

## Actual broker and Home Assistant lifecycle

[Recorded results](validation-results/lifecycle.json) verify:

- Gateway and HA command publications are denied with MQTT 5 PUBACK 135.
- The gateway broker principal receives no subscribed telemetry.
- An actual HA `alarm_disarm` service call returns HTTP 200 but leaves the alarm
  disarmed and sends no control opcode to the simulator.
- Panel disconnect and malformed frames cause unavailability and fresh recovery.
- Graceful shutdown and SIGKILL both revoke availability; restart restores states.
- Broker restart recovers, and HA restart restores retained discovery and state.

The independent [negative PUBACK probe](validation-results/negative-puback.json)
verifies the production publisher's rejection path against real Mosquitto.
After rejection, DISCONNECT-with-Will produces offline availability both live
and for a fresh subscriber reading retained data. This catches the dangerous
failure mode where a normal graceful disconnect would suppress the will.

## Measured latency and delivery

100 individually injected transitions during the lifecycle test measured
**1.290 ms p50, 1.957 ms p95, 2.674 ms maximum** from simulator TLS event emission
to Home Assistant's state update timestamp.

The final [load result](validation-results/benchmark.json) delivered **6,600 of
6,600 transitions**, with all 6,600 MQTT acknowledgements completed, zero queue
overflows, zero reconnects and zero failures during the workload. The test took
8.908 seconds, delivering 740.91 updates/second under an HTTP-driven offered load.
This is not a measurement of maximum sustainable protocol throughput.

| Workload | p50 | p95 | p99 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| Initial 600-point burst | 0.382 ms | 2.060 ms | 5.693 ms | 8.638 ms |
| Subsequent 6,000 transitions | 0.346 ms | 2.628 ms | 103.626 ms | 168.333 ms |
| All 6,600 transitions | 0.349 ms | 2.555 ms | 94.455 ms | 168.333 ms |

The gateway's callback-to-PUBACK distribution over its final 4,096-observation
window was 1.547 ms p50, 3.028 ms p95 and 4.229 ms p99, maximum 42.322 ms. This
window differs from the complete pipeline sample and must not be substituted
for the end-to-end figures. Earlier runs also showed intermittent long pipeline
tails (up to 163 ms). These measurements establish low typical latency and
complete delivery under this workload; they do **not** establish a sub-10 ms
worst-case guarantee. HA scheduling/storage, VM contention and the physical
network must be measured during commissioning.

After the run, the gateway used approximately 44 MiB RAM; a single idle CPU
snapshot was 0.29%. These are not peak resource measurements or a long-term
memory soak. The methodology and configurable p99 regression gate are described
in [performance-method.md](performance-method.md). Per-transition timestamps
remain available locally in ignored `dev/generated/benchmark-samples.json`.

## Scope and reproduction

`sh dev/check-lifecycle.sh` runs the complete Docker lifecycle and load test.
Run unit checks with `uv run --frozen pytest`; the TLS integration test needs
permission to bind/connect loopback sockets. CI includes Python 3.12–3.14 and
the real Docker lifecycle, but the hosted GitHub workflow has not been run here.

No physical B9512G, production broker, or production HA instance was contacted.
The systemd deployment and VM firewall policy are supplied for installation;
they have not been commissioned on the user's dedicated VM. Confirm actual
firmware responses, certificate identity, account permissions and outage timing
before production cutover. Initial area readiness remains unknown until the
panel emits a readiness event; this avoids inventing a safe state.

## Explicit plaintext option — follow-up validation

The subsequent isolated-network MQTT option passed all **149 tests on Python
3.13.14**, with 93% statement coverage, Ruff lint/format checks and Mypy. Tests
verify explicit transport selection, unchanged TLS defaults, rejection of
contradictory TLS-file settings and no automatic downgrade. Panel TLS remains
mandatory. The earlier Python matrix and HA performance figures above describe
the original TLS deployment; the full HA benchmark was not rerun for this option.

A [real Mosquitto plaintext probe](validation-results/plaintext.json) verified
authentication, retained QoS 1 state, rejected commands, retained offline Will
after negative PUBACK, wrong-password rejection and failure of default TLS against
a plaintext-only listener. The probe used a separate disposable broker bound to
an ephemeral loopback port, removed it afterwards, and is included in
`dev/check-lifecycle.sh`. The existing real TLS negative-PUBACK probe also passed
after the transport change. No production broker or panel was contacted.
