"""An outbound-only MQTT capability; deliberately no subscription interface."""

from __future__ import annotations

import asyncio
import socket
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import aiomqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from .config import GatewayConfig, MQTTConfig


class PublishRejected(aiomqtt.MqttError):
    """A broker rejected a publication; availability must fail closed."""


class CheckedClient(aiomqtt.Client):
    """aiomqtt 2.5.1 otherwise discards MQTT 5 negative PUBACK reason codes.

    This narrow compatibility hook is covered by tests and pinned by uv.lock.
    A single rejection poisons the session so no later success can imply readiness.
    """

    rejected = False

    def _on_publish(self, client, userdata, mid, reason_code, properties):
        if reason_code.is_failure:
            self.rejected = True
        super()._on_publish(client, userdata, mid, reason_code, properties)

    async def publish(self, *args, **kwargs):
        if self.rejected:
            raise PublishRejected("broker rejected publication")
        await super().publish(*args, **kwargs)
        if self.rejected:
            raise PublishRejected("broker rejected publication")

    async def disconnect_with_will(self, deadline_seconds: float) -> None:
        """A graceful normal DISCONNECT would leave a retained online value behind."""
        self._client.disconnect(reasoncode=ReasonCode(PacketTypes.DISCONNECT, identifier=4))
        try:
            async with asyncio.timeout(deadline_seconds):
                await asyncio.shield(self._disconnected)
        except (TimeoutError, aiomqtt.MqttError):
            # A dead transport cannot acknowledge revocation; force broker-side will handling.
            connection = self._client.socket()
            if isinstance(connection, socket.socket):
                with suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)


class Publisher:
    """Restricts all outgoing publications to this instance's owned topics."""

    def __init__(self, client: CheckedClient, gateway: GatewayConfig):
        self._client = client
        self._gateway = gateway
        self.offline_confirmed = False

    async def wait_disconnected(self) -> None:
        """Observe transport loss without subscribing to any MQTT topic."""
        await asyncio.shield(self._client._disconnected)
        raise aiomqtt.MqttError("MQTT transport disconnected")

    def allows(self, topic: str) -> bool:
        base = self._gateway.topic_prefix
        if topic in (f"{base}/availability", f"{base}/panel_availability"):
            return True
        if topic.startswith(f"{base}/state/"):
            suffix = topic[len(base) + 7 :]
            return bool(suffix) and all(c.isalnum() or c in "_-/" for c in suffix)
        prefix = self._gateway.discovery_prefix + "/"
        if topic.startswith(prefix):
            parts = topic[len(prefix) :].split("/")
            return (
                len(parts) == 4
                and parts[0] in {"alarm_control_panel", "binary_sensor", "sensor"}
                and parts[1] == self._gateway.id
                and bool(parts[2])
                and all(c.isalnum() or c in "_-" for c in parts[2])
                and parts[3] == "config"
            )
        return False

    async def publish(self, topic: str, payload: str | bytes, *, retain: bool = True) -> None:
        if not self.allows(topic):
            raise ValueError("publication outside owned telemetry/discovery namespace")
        availability = topic == f"{self._gateway.topic_prefix}/availability"
        if availability and payload in ("online", b"online"):
            # Invalidate before sending: the broker may apply a packet whose ACK is lost.
            self.offline_confirmed = False
        await self._client.publish(topic, payload, qos=1, retain=retain)
        if availability and payload in ("offline", b"offline"):
            self.offline_confirmed = True


def tls_context(config: MQTTConfig) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=config.ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if config.cert_file:
        context.load_cert_chain(config.cert_file, config.key_file)
    return context


@asynccontextmanager
async def connect_publisher(
    config: MQTTConfig, gateway: GatewayConfig, password: str
) -> AsyncIterator[Publisher]:
    properties = Properties(PacketTypes.CONNECT)
    properties.SessionExpiryInterval = 0
    async with CheckedClient(
        config.host,
        config.port,
        username=config.username,
        password=password,
        identifier=f"bosch-gateway-{gateway.id}",
        protocol=aiomqtt.ProtocolVersion.V5,
        clean_start=True,
        properties=properties,
        tls_context=tls_context(config),
        keepalive=config.keepalive,
        timeout=config.timeout,
        will=aiomqtt.Will(f"{gateway.topic_prefix}/availability", "offline", qos=1, retain=True),
        max_queued_incoming_messages=1,
        max_queued_outgoing_messages=gateway.publish_workers * 2,
        max_inflight_messages=gateway.publish_workers,
        max_concurrent_outgoing_calls=gateway.publish_workers,
        socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)],
    ) as client:
        publisher = Publisher(client, gateway)
        try:
            yield publisher
        finally:
            if not publisher.offline_confirmed:
                await client.disconnect_with_will(config.timeout)
