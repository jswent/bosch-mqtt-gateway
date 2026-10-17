"""Bounded metrics and deliberately conservative security logging."""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from datetime import UTC, datetime


class SafeFormatter(logging.Formatter):
    def __init__(self, secrets: tuple[str, ...], json_format: bool = True):
        super().__init__()
        self.secrets = secrets
        self.json_format = json_format

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        for value in self.secrets:
            if value:
                message = message.replace(value, "[REDACTED]")
        # Exception messages/tracebacks can embed credentials or complete protocol frames.
        item = {
            "time": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": message,
        }
        if record.exc_info and record.exc_info[0]:
            item["error_type"] = record.exc_info[0].__name__
        if self.json_format:
            return json.dumps(item, ensure_ascii=True)
        return " ".join(f"{key}={json.dumps(value)}" for key, value in item.items())


class NoWireLogging(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Upstream DEBUG logs contain authentication packets and physical security state.
        return not record.name.startswith(("bosch_alarm_mode2", "paho.mqtt"))


def configure_logging(level: str, format: str, secrets: tuple[str, ...]) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter(secrets, format == "json"))
    handler.addFilter(NoWireLogging())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.getLogger("uvicorn.access").disabled = True


class Metrics:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.events_received = 0
        self.events_published = 0
        self.events_deduplicated = 0
        self.queue_overflows = 0
        self.reconnections = 0
        self.failures = 0
        self._latencies: deque[float] = deque(maxlen=4096)

    def published(self, received: float) -> None:
        self.events_published += 1
        self._latencies.append((time.monotonic() - received) * 1000)

    def snapshot(self) -> dict[str, int | float]:
        ordered = sorted(self._latencies)

        def percentile(p: float) -> float:
            return (
                round(ordered[min(int((len(ordered) - 1) * p), len(ordered) - 1)], 3)
                if ordered
                else 0.0
            )

        return {
            "uptime_seconds": round(time.monotonic() - self.started, 2),
            "events_received": self.events_received,
            "events_published": self.events_published,
            "events_deduplicated": self.events_deduplicated,
            "queue_overflows": self.queue_overflows,
            "reconnections": self.reconnections,
            "failures": self.failures,
            "publish_latency_samples": len(ordered),
            "publish_latency_p50_ms": percentile(0.50),
            "publish_latency_p95_ms": percentile(0.95),
            "publish_latency_p99_ms": percentile(0.99),
            "publish_latency_max_ms": max(ordered, default=0.0),
        }
