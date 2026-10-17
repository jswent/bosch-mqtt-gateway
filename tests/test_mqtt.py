import asyncio
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from bosch_mqtt_gateway.config import GatewayConfig, MQTTConfig
from bosch_mqtt_gateway.mqtt import CheckedClient, Publisher, PublishRejected, tls_context


def publisher():
    client = SimpleNamespace(publish=AsyncMock())
    return Publisher(client, GatewayConfig(id="mine", topic_prefix="bosch/mine")), client


@pytest.mark.parametrize(
    "topic",
    [
        "bosch/mine/availability",
        "bosch/mine/panel_availability",
        "bosch/mine/state/point/600",
        "homeassistant/alarm_control_panel/mine/area1/config",
        "homeassistant/binary_sensor/mine/point600/config",
    ],
)
async def test_owned_publications(topic):
    pub, client = publisher()
    await pub.publish(topic, "data")
    client.publish.assert_awaited_once_with(topic, "data", qos=1, retain=True)


@pytest.mark.parametrize(
    "topic",
    [
        "bosch/mine/command/area/1",
        "bosch/mine/state/#",
        "bosch/mine/state/",
        "bosch/other/state/point/1",
        "$SYS/broker",
        "homeassistant/switch/mine/a/config",
        "homeassistant/sensor/other/a/config",
        "homeassistant/sensor/mine/+/config",
        "homeassistant/sensor/mine/a/other",
        "homeassistant/sensor/mine/a/config/extra",
    ],
)
async def test_cannot_publish_commands_or_escape_namespace(topic):
    pub, client = publisher()
    with pytest.raises(ValueError):
        await pub.publish(topic, "ARM_AWAY")
    client.publish.assert_not_called()
    assert not hasattr(pub, "subscribe")


async def test_negative_puback_poisons_session():
    client = CheckedClient("localhost")
    event = asyncio.Event()
    client._pending_publishes[123] = event
    client._on_publish(
        None,
        None,
        123,
        ReasonCode(PacketTypes.PUBACK, "Not authorized"),
        Properties(PacketTypes.PUBACK),
    )
    assert event.is_set()
    with pytest.raises(PublishRejected):
        await client.publish("anything", "payload")


def test_mqtt_trust_defaults():
    context = tls_context(MQTTConfig(host="localhost", username="a", password_file="b"))
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
