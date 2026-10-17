import asyncio
import json
import time
from contextlib import asynccontextmanager

import pytest

from bosch_mqtt_gateway.models import EntityState, PanelInfo
from bosch_mqtt_gateway.mqtt import Publisher
from bosch_mqtt_gateway.runtime import Gateway, Secrets

SECRETS = Secrets("panel-secret", None, "mqtt-secret", "d" * 48)


def point(number, state=False):
    return EntityState(
        kind="point", id=str(number), name=f"Point {number}", state=state, attributes={}
    )


class FakePanel:
    count = 600
    instances = []

    def __init__(self, *args, on_state, on_availability, **kwargs):
        self.on_state = on_state
        self.on_availability = on_availability
        self.info = PanelInfo(serial="123456", model="B9512G", firmware="3.14")
        self.stopped = False
        self.instances.append(self)

    async def start(self):
        self.on_availability(True)

    def snapshot(self):
        return tuple(point(i) for i in range(1, self.count + 1))

    async def stop(self):
        self.stopped = True


class Recorder:
    def __init__(self, config):
        self.messages = []
        self.allows = Publisher(None, config.gateway).allows
        self.hook = None

    async def publish(self, topic, payload, **kwargs):
        assert self.allows(topic)
        await asyncio.sleep(0)
        self.messages.append((topic, payload))
        if self.hook:
            self.hook(topic, payload)

    @asynccontextmanager
    async def context(self, *args):
        yield self


async def until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


async def launch(config):
    recorder = Recorder(config)
    gateway = Gateway(config, SECRETS, panel_factory=FakePanel, publisher_factory=recorder.context)
    task = asyncio.create_task(gateway.run())
    await until(lambda: gateway.ready)
    return gateway, recorder, task


async def shutdown(gateway, task):
    gateway.request_stop()
    async with asyncio.timeout(3):
        await task


async def test_initial_snapshot_precedes_online_and_graceful_shutdown(config):
    gateway, recorder, task = await launch(config)
    try:
        assert gateway.entity_count == 600
        online = recorder.messages.index(("bosch/test/availability", "online"))
        assert sum("/state/" in topic for topic, _ in recorder.messages[:online]) == 600
        assert recorder.messages[0] == ("bosch/test/availability", "offline")
        assert gateway.status()["commands_enabled"] is False
    finally:
        await shutdown(gateway, task)
    assert recorder.messages[-2:] == [
        ("bosch/test/availability", "offline"),
        ("bosch/test/panel_availability", "offline"),
    ]
    assert FakePanel.instances[-1].stopped


@pytest.mark.performance
async def test_600_point_burst_preserves_all_transitions_and_order(config):
    gateway, recorder, task = await launch(config)
    try:
        recorder.messages.clear()
        start = time.monotonic()
        for new_state in (True, False):
            for number in range(1, 601):
                gateway._on_state(point(number, new_state))
        await until(lambda: gateway.metrics.events_published == 1200)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0  # Functional regression limit; full-stack latency measured separately.
        topic = gateway._builder.state_topic(point(600))
        updates = [
            json.loads(payload)["state"] for name, payload in recorder.messages if name == topic
        ]
        assert updates == [True, False]
        assert gateway.metrics.queue_overflows == 0
    finally:
        await shutdown(gateway, task)


async def test_disconnect_revokes_ready_and_refreshes_snapshot(config):
    gateway, recorder, task = await launch(config)
    try:
        prior = FakePanel.instances[-1]
        prior.on_availability(False)
        assert not gateway.ready
        await until(lambda: FakePanel.instances[-1] is not prior and gateway.ready)
        assert prior.stopped
        assert gateway.metrics.reconnections == 1
        assert recorder.messages.count(("bosch/test/panel_availability", "offline")) >= 2
    finally:
        await shutdown(gateway, task)


async def test_overflow_fails_closed_instead_of_silently_losing_state(config):
    gateway = Gateway(config, SECRETS)
    gateway._accepting = True
    gateway.ready = True
    gateway._queues = [asyncio.Queue(maxsize=1)]
    gateway._on_state(point(1, True))
    gateway._on_state(point(1, False))
    assert not gateway.ready
    assert gateway._overflow.is_set()
    assert gateway.metrics.queue_overflows == 1
    gateway._on_state(point(1, True))
    assert gateway.metrics.queue_overflows == 1
    assert gateway._queues[0].qsize() == 1


async def test_duplicate_states_deduplicated_without_losing_changes(config):
    gateway, recorder, task = await launch(config)
    try:
        gateway._on_state(point(1, True))
        gateway._on_state(point(1, True))
        gateway._on_state(point(1, False))
        await until(lambda: gateway.metrics.events_published == 2)
        assert gateway.metrics.events_deduplicated == 1
    finally:
        await shutdown(gateway, task)


async def test_updates_during_discovery_applied_after_snapshot(config):
    recorder = Recorder(config)
    gateway = Gateway(config, SECRETS, panel_factory=FakePanel, publisher_factory=recorder.context)
    sent = False

    def inject(topic, payload):
        nonlocal sent
        if "/config" in topic and not sent:
            sent = True
            gateway._on_state(point(1, True))

    recorder.hook = inject
    task = asyncio.create_task(gateway.run())
    try:
        await until(lambda: gateway.ready)
        topic = gateway._builder.state_topic(point(1))
        states = [
            json.loads(payload)["state"] for name, payload in recorder.messages if name == topic
        ]
        assert states == [False, True]
        online = recorder.messages.index(("bosch/test/availability", "online"))
        update = recorder.messages.index((topic, gateway._builder.state_payload(point(1, True))))
        assert update < online
    finally:
        await shutdown(gateway, task)


async def test_removed_entities_emit_retained_tombstones(config):
    gateway, recorder, task = await launch(config)
    await shutdown(gateway, task)
    previous = {topic for topic, _ in recorder.messages if "/config" in topic or "/state/" in topic}

    class SmallerPanel(FakePanel):
        count = 1

    next_recorder = Recorder(config)
    next_gateway = Gateway(
        config, SECRETS, panel_factory=SmallerPanel, publisher_factory=next_recorder.context
    )
    next_task = asyncio.create_task(next_gateway.run())
    try:
        await until(lambda: next_gateway.ready)
        tombstones = {topic for topic, payload in next_recorder.messages if payload == ""}
        assert len(tombstones) == 1198
        assert tombstones <= previous
    finally:
        await shutdown(next_gateway, next_task)
