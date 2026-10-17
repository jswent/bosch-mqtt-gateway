"""Real broker negative PUBACK poisons session and emits retained offline Will."""

import asyncio
import json
import ssl
from pathlib import Path

import aiomqtt

from bosch_mqtt_gateway.config import GatewayConfig, MQTTConfig
from bosch_mqtt_gateway.mqtt import PublishRejected, connect_publisher

ROOT = Path(__file__).resolve().parent / "generated"


async def main():
    config = MQTTConfig(
        host="localhost",
        port=18883,
        username="gateway",
        password_file=str(ROOT / "mqtt_gateway_password"),
        ca_file=str(ROOT / "ca.crt"),
    )
    gateway = GatewayConfig(id="ack_probe", topic_prefix="bosch/dev/state/ack_probe")
    topic = gateway.topic_prefix + "/availability"
    async with aiomqtt.Client(
        "localhost",
        18883,
        username="homeassistant",
        password=(ROOT / "mqtt_ha_password").read_text(),
        protocol=aiomqtt.ProtocolVersion.V5,
        tls_context=ssl.create_default_context(cafile=str(ROOT / "ca.crt")),
    ) as observer:
        await observer.subscribe(topic, qos=1)
        poisoned = False
        try:
            async with connect_publisher(
                config, gateway, (ROOT / "mqtt_gateway_password").read_text()
            ) as publisher:
                await publisher.publish(topic, "online")
                async with asyncio.timeout(5):
                    async for message in observer.messages:
                        if message.payload == b"online":
                            break
                await publisher._client.publish("bosch/dev/command/area/probe", "IGNORED", qos=1)
        except PublishRejected:
            poisoned = True
        assert poisoned
        async with asyncio.timeout(10):
            async for message in observer.messages:
                if message.payload == b"offline":
                    break
        # Confirm the Will was retained for a fresh subscriber, not merely delivered live.
        await observer.unsubscribe(topic)
        await observer.subscribe(topic, qos=1)
        async with asyncio.timeout(10):
            async for message in observer.messages:
                if message.payload == b"offline" and message.retain:
                    break
        async with connect_publisher(
            config, gateway, (ROOT / "mqtt_gateway_password").read_text()
        ) as publisher:
            await publisher.publish(topic, "offline")
            await publisher.publish(topic, "")
    result = {"negative_puback_rejected": True, "disconnect_with_will_retained_offline": True}
    (ROOT / "puback-results.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
