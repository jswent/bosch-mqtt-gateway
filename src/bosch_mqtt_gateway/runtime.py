"""Supervised telemetry lifecycle with bounded queues and per-entity ordering."""

from __future__ import annotations

import asyncio
import logging
import random
import time
import zlib
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from .config import Config
from .discovery import DiscoveryBuilder
from .models import EntityState
from .mqtt import Publisher, connect_publisher
from .observability import Metrics
from .panel import PanelAdapter
from .storage import TopicJournal

LOG = logging.getLogger(__name__)
AUDIT = logging.getLogger("bosch_mqtt_gateway.audit")


class PanelDisconnected(Exception):
    """The current snapshot is no longer trustworthy."""


class QueueOverflow(Exception):
    """A telemetry transition could not be queued; reconnect and resnapshot."""


@dataclass(repr=False, frozen=True)
class Secrets:
    panel_code: str
    user_code: str | None
    mqtt_password: str
    debug_token: str | None


class Gateway:
    def __init__(
        self,
        config: Config,
        secrets: Secrets,
        *,
        panel_factory: Callable[..., Any] = PanelAdapter,
        publisher_factory: Callable[..., Any] = connect_publisher,
    ) -> None:
        self.config = config
        self.secrets = secrets
        self.metrics = Metrics()
        self.ready = False
        self.broker_connected = False
        self.panel_connected = False
        self.entity_count = 0
        self.last_error: str | None = None
        self.phase = "starting"
        self._panel_factory = panel_factory
        self._publisher_factory = publisher_factory
        self._latest: dict[tuple[str, str], EntityState] = {}
        self._queues: list[asyncio.Queue[tuple[EntityState, float]]] = []
        self._accepting = False
        self._panel_down = asyncio.Event()
        self._overflow = asyncio.Event()
        self._stop = asyncio.Event()
        self._builder = DiscoveryBuilder(
            gateway_id=config.gateway.id,
            topic_prefix=config.gateway.topic_prefix,
            discovery_prefix=config.gateway.discovery_prefix,
        )

    def status(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "broker_connected": self.broker_connected,
            "panel_connected": self.panel_connected,
            "mode": "publish_only",
            "mqtt_subscriptions": 0,
            "commands_enabled": False,
            "entity_count": self.entity_count,
            "queued_events": sum(queue.qsize() for queue in self._queues),
            "last_error": self.last_error,
            "phase": self.phase,
            "metrics": self.metrics.snapshot(),
        }

    def debug_states(self) -> list[dict[str, Any]]:
        return [
            {
                "kind": state.kind,
                "id": state.id,
                "name": state.name,
                "state": state.state,
                "attributes": dict(state.attributes),
            }
            for state in self._latest.values()
        ]

    def request_stop(self) -> None:
        self.ready = False
        self._stop.set()

    def _on_state(self, state: EntityState) -> None:
        if not self._accepting or self._overflow.is_set() or self._panel_down.is_set():
            return
        key = (state.kind, str(state.id))
        self._latest[key] = state
        self.metrics.events_received += 1
        # Same entity always uses the same FIFO worker; distinct entities can publish concurrently.
        partition = zlib.crc32(f"{key[0]}/{key[1]}".encode()) % len(self._queues)
        try:
            self._queues[partition].put_nowait((state, time.monotonic()))
        except asyncio.QueueFull:
            self.metrics.queue_overflows += 1
            self.ready = False
            self._overflow.set()

    def _on_availability(self, online: bool) -> None:
        if not online and self._accepting:
            self.panel_connected = False
            self.ready = False
            self._panel_down.set()

    async def _worker(self, queue: asyncio.Queue, publisher: Publisher) -> None:
        previous: dict[str, str] = {}
        while True:
            state, received = await queue.get()
            try:
                topic = self._builder.state_topic(state)
                payload = self._builder.state_payload(state)
                if previous.get(topic) == payload:
                    self.metrics.events_deduplicated += 1
                    continue
                await publisher.publish(topic, payload)
                previous[topic] = payload
                self.metrics.published(received)
            finally:
                queue.task_done()

    async def _watch_broker(self, publisher: Publisher) -> None:
        try:
            await publisher.wait_disconnected()
        finally:
            self.ready = False
            self.broker_connected = False

    async def _heartbeat(self, publisher: Publisher) -> None:
        while True:
            await asyncio.sleep(self.config.gateway.heartbeat_interval)
            await publisher.publish(
                f"{self.config.gateway.topic_prefix}/availability",
                "online" if self.ready else "offline",
            )

    async def _watch(self, event: asyncio.Event, error: type[Exception]) -> None:
        await event.wait()
        raise error()

    async def _batch(self, publisher: Publisher, messages: dict[str, str]) -> None:
        items = list(messages.items())
        size = self.config.gateway.publish_workers
        # Bounded batches avoid thousands of publish tasks during initial discovery.
        for offset in range(0, len(items), size):
            async with asyncio.TaskGroup() as group:
                for topic, payload in items[offset : offset + size]:
                    group.create_task(publisher.publish(topic, payload))

    async def _synchronize(self, panel: Any, publisher: Publisher) -> None:
        states = panel.snapshot()
        self._latest = {(state.kind, str(state.id)): state for state in states}
        self.entity_count = len(states)
        # No await between snapshot and enabling callbacks: later deltas cannot be lost.
        self._accepting = True
        configs = self._builder.build(panel.info, states)
        snapshots = {
            self._builder.state_topic(state): self._builder.state_payload(state) for state in states
        }
        current_topics = set(configs) | set(snapshots)
        journal = TopicJournal(self.config.gateway)
        previous_topics = journal.load(publisher.allows)
        LOG.debug(
            "discovery_sync states=%d entities=%d removed_topics=%d",
            len(states),
            len(configs),
            len(previous_topics - current_topics),
        )
        # Journal the union before publishing so a crash cannot orphan newly retained entities.
        await asyncio.to_thread(journal.save, previous_topics | current_topics)
        await self._batch(publisher, dict.fromkeys(previous_topics - current_topics, ""))
        await self._batch(publisher, configs)
        await self._batch(publisher, snapshots)
        await asyncio.to_thread(journal.save, current_topics)

    async def _session(self, publisher: Publisher) -> None:
        self._panel_down.clear()
        self._overflow.clear()
        size = self.config.gateway.queue_capacity // self.config.gateway.publish_workers
        self._queues = [asyncio.Queue(size) for _ in range(self.config.gateway.publish_workers)]
        cfg = self.config.panel
        panel = self._panel_factory(
            cfg.host,
            cfg.port,
            self.secrets.panel_code,
            self.secrets.user_code,
            ca_file=cfg.ca_file,
            server_name=cfg.server_name,
            certificate_sha256=cfg.certificate_sha256,
            on_state=self._on_state,
            on_availability=self._on_availability,
            timeout=cfg.timeout,
        )
        try:
            self.phase = "connecting_panel"
            LOG.debug("panel_session_start")
            await panel.start()
            self.panel_connected = True
            if self._stop.is_set():
                return
            self.phase = "synchronizing"
            await self._synchronize(panel, publisher)
            async with asyncio.TaskGroup() as group:
                workers = [
                    group.create_task(self._worker(queue, publisher)) for queue in self._queues
                ]
                monitors = [
                    group.create_task(self._watch(self._panel_down, PanelDisconnected)),
                    group.create_task(self._watch(self._overflow, QueueOverflow)),
                    group.create_task(self._heartbeat(publisher)),
                    group.create_task(self._watch_broker(publisher)),
                ]
                # Catch up deltas that arrived while retained discovery was being published.
                async with asyncio.timeout(cfg.timeout):
                    await asyncio.gather(*(queue.join() for queue in self._queues))
                if self._stop.is_set():
                    for task in workers + monitors:
                        task.cancel()
                    return
                if self._panel_down.is_set() or self._overflow.is_set():
                    raise PanelDisconnected()
                await publisher.publish(
                    f"{self.config.gateway.topic_prefix}/panel_availability", "online"
                )
                await publisher.publish(
                    f"{self.config.gateway.topic_prefix}/availability", "online"
                )
                if self._panel_down.is_set() or self._overflow.is_set():
                    raise PanelDisconnected()
                self.ready = True
                self.phase = "publishing"
                self.last_error = None
                AUDIT.info("telemetry_ready entities=%d commands_enabled=false", self.entity_count)
                await self._stop.wait()
                for task in workers + monitors:
                    task.cancel()
        finally:
            self._accepting = False
            self.ready = False
            self.panel_connected = False
            await panel.stop()

    async def run(self) -> None:
        delay = self.config.gateway.reconnect_min
        while not self._stop.is_set():
            session_started = time.monotonic()
            try:
                self.phase = "connecting_mqtt"
                LOG.debug("mqtt_session_start")
                async with self._publisher_factory(
                    self.config.mqtt, self.config.gateway, self.secrets.mqtt_password
                ) as publisher:
                    self.broker_connected = True
                    try:
                        await publisher.publish(
                            f"{self.config.gateway.topic_prefix}/availability", "offline"
                        )
                        await publisher.publish(
                            f"{self.config.gateway.topic_prefix}/panel_availability", "offline"
                        )
                        await self._session(publisher)
                    finally:
                        self.ready = False
                        self.panel_connected = False
                        # Revoke availability before a graceful disconnect suppresses the will.
                        with suppress(Exception):
                            async with asyncio.timeout(self.config.mqtt.timeout):
                                await publisher.publish(
                                    f"{self.config.gateway.topic_prefix}/availability", "offline"
                                )
                                await publisher.publish(
                                    f"{self.config.gateway.topic_prefix}/panel_availability",
                                    "offline",
                                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.metrics.failures += 1
                errors = exc.exceptions if isinstance(exc, ExceptionGroup) else (exc,)
                self.last_error = ",".join(sorted({type(error).__name__ for error in errors}))
                LOG.warning("session_failed phase=%s error_type=%s", self.phase, self.last_error)
            finally:
                self.broker_connected = False
                self.ready = False
                self.panel_connected = False
                self._accepting = False
            if self._stop.is_set():
                break
            self.metrics.reconnections += 1
            self.phase = "reconnect_delay"
            if time.monotonic() - session_started > 60:
                delay = self.config.gateway.reconnect_min
            try:
                async with asyncio.timeout(random.uniform(delay / 2, delay)):
                    await self._stop.wait()
            except TimeoutError:
                pass
            delay = min(delay * 2, self.config.gateway.reconnect_max)
        self.phase = "stopped"
