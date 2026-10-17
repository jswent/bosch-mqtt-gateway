"""Independent regressions for failure/replay races in the session supervisor."""

import asyncio

import pytest
from test_runtime import SECRETS, FakePanel, Recorder, point, shutdown, until

from bosch_mqtt_gateway.runtime import Gateway
from bosch_mqtt_gateway.storage import TopicJournal


async def test_partial_snapshot_failure_replays_full_discovery(config):
    recorder = Recorder(config)
    gateway = Gateway(config, SECRETS, panel_factory=FakePanel, publisher_factory=recorder.context)
    original = recorder.publish
    failed = False
    split = 0

    async def publish(topic, payload, **kwargs):
        nonlocal failed, split
        if "/state/" in topic and not failed:
            failed = True
            split = len(recorder.messages)
            raise ConnectionError("injected publication failure")
        await original(topic, payload, **kwargs)

    recorder.publish = publish
    task = asyncio.create_task(gateway.run())
    try:
        await until(lambda: gateway.ready)
        assert failed
        replay = recorder.messages[split:]
        configs = {topic for topic, payload in replay if topic.endswith("/config") and payload}
        snapshots = {topic for topic, payload in replay if "/state/" in topic and payload}
        assert len(configs) == 601  # 600 points plus the root connection diagnostic.
        assert len(snapshots) == 600
        assert gateway.metrics.failures == 1
        assert gateway.metrics.reconnections == 1
        assert len(TopicJournal(config.gateway).load(recorder.allows)) == 1201
    finally:
        await shutdown(gateway, task)


async def test_disconnect_during_online_publish_never_marks_ready(config):
    recorder = Recorder(config)
    gateway = Gateway(config, SECRETS, panel_factory=FakePanel, publisher_factory=recorder.context)
    disconnected = False

    def disconnect(topic, payload):
        nonlocal disconnected
        if topic.endswith("/panel_availability") and payload == "online":
            disconnected = True
            gateway._on_availability(False)

    recorder.hook = disconnect
    with pytest.raises(ExceptionGroup):
        await gateway._session(recorder)
    assert disconnected
    assert not gateway.ready
    assert not gateway.panel_connected
    assert FakePanel.instances[-1].stopped


async def test_journal_cannot_delete_other_gateways_topics(config):
    recorder = Recorder(config)
    journal = TopicJournal(config.gateway)
    journal.save({"homeassistant/sensor/someone_else/secret/config"})
    gateway = Gateway(config, SECRETS, panel_factory=FakePanel, publisher_factory=recorder.context)
    with pytest.raises(ValueError, match="unowned"):
        await gateway._session(recorder)
    assert recorder.messages == []
    assert not gateway.ready


async def test_worker_failure_does_not_suppress_later_replay(config):
    recorder = Recorder(config)
    gateway = Gateway(config, SECRETS, panel_factory=FakePanel, publisher_factory=recorder.context)
    task = asyncio.create_task(gateway.run())
    try:
        await until(lambda: gateway.ready)
        original = recorder.publish
        failed = False

        async def publish(topic, payload, **kwargs):
            nonlocal failed
            if "/state/" in topic and not failed:
                failed = True
                raise ConnectionError("injected live update failure")
            await original(topic, payload, **kwargs)

        recorder.publish = publish
        previous = FakePanel.instances[-1]
        gateway._on_state(point(1, True))
        await until(lambda: gateway.ready and FakePanel.instances[-1] is not previous)
        assert previous.stopped
        assert gateway.metrics.failures == 1
        assert gateway.entity_count == 600
        assert recorder.messages.count(("bosch/test/availability", "online")) == 2
    finally:
        await shutdown(gateway, task)


async def test_rejected_session_requests_will_before_graceful_exit(config, monkeypatch):
    from unittest.mock import AsyncMock

    from bosch_mqtt_gateway import mqtt

    class Client:
        def __init__(self, *args, **kwargs):
            self.publish = AsyncMock()
            self.disconnect_with_will = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.disconnect_with_will.assert_awaited_once()

    client = Client()
    monkeypatch.setattr(mqtt, "CheckedClient", lambda *a, **kw: client)
    async with mqtt.connect_publisher(config.mqtt, config.gateway, "secret") as publisher:
        topic = f"{config.gateway.topic_prefix}/availability"
        await publisher.publish(topic, "offline")
        assert publisher.offline_confirmed
        await publisher.publish(topic, "online")
        assert not publisher.offline_confirmed
        client.publish.side_effect = mqtt.PublishRejected("rejected")
        with pytest.raises(mqtt.PublishRejected):
            await publisher.publish(topic, "offline")
    client.disconnect_with_will.assert_awaited_once_with(config.mqtt.timeout)


async def test_lost_online_ack_invalidates_confirmed_offline(config):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from bosch_mqtt_gateway.mqtt import Publisher

    client = SimpleNamespace(publish=AsyncMock())
    publisher = Publisher(client, config.gateway)
    topic = f"{config.gateway.topic_prefix}/availability"
    await publisher.publish(topic, "offline")
    assert publisher.offline_confirmed
    client.publish.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await publisher.publish(topic, "online")
    assert not publisher.offline_confirmed
