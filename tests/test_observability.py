import logging
import time

from bosch_mqtt_gateway.observability import Metrics, NoWireLogging, SafeFormatter


def test_redaction_and_no_exception_payload():
    secret = "secret-password"
    record = logging.LogRecord(
        "gateway",
        logging.ERROR,
        "",
        1,
        f"failed {secret}",
        (),
        (ValueError, ValueError(secret), None),
    )
    output = SafeFormatter((secret,)).format(record)
    assert secret not in output
    assert "[REDACTED]" in output
    assert "ValueError" in output


def test_protocol_logs_suppressed():
    for name in ["bosch_alarm_mode2.connection", "bosch_alarm_mode2.panel", "paho.mqtt.client"]:
        record = logging.LogRecord(name, logging.DEBUG, "", 1, "raw secret frame", (), None)
        assert not NoWireLogging().filter(record)
    assert NoWireLogging().filter(
        logging.LogRecord("bosch_mqtt_gateway.audit", logging.INFO, "", 1, "safe event", (), None)
    )


def test_latency_metrics_bounded():
    metrics = Metrics()
    for _ in range(10000):
        metrics.published(time.monotonic())
    snapshot = metrics.snapshot()
    assert snapshot["publish_latency_samples"] == 4096
    assert snapshot["events_published"] == 10000
    assert snapshot["publish_latency_p99_ms"] >= 0
