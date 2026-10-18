"""Test explicit plaintext against a disposable loopback-only broker, never the lab broker."""

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import aiomqtt

from bosch_mqtt_gateway.config import GatewayConfig, MQTTConfig
from bosch_mqtt_gateway.mqtt import PublishRejected, connect_publisher

ROOT = Path(__file__).resolve().parent / "generated"
DOCKER = shutil.which("docker") or "/usr/local/bin/docker"
IMAGE = (
    "eclipse-mosquitto:2.0.22@sha256:"
    "199ea8ef2e35ec2b1b37e59cfd1dbae538ed4dfa4a2251a121a52215a6248a21"
)


async def docker(*args):
    process = await asyncio.create_subprocess_exec(
        DOCKER, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        async with asyncio.timeout(90):
            output, error = await process.communicate()
    except BaseException:
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError(f"Docker failed: {error.decode().strip()}")
    return output.decode().strip()


async def wait_ready(port):
    async with asyncio.timeout(15):
        while True:
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(0.2)


async def receive(client, topic, payload, *, retained=False):
    async with asyncio.timeout(10):
        async for message in client.messages:
            if (
                str(message.topic) == topic
                and message.payload == payload
                and (not retained or message.retain)
            ):
                return


async def probe(port):
    password = (ROOT / "mqtt_gateway_password").read_text().strip()
    config = MQTTConfig(
        transport="plaintext",
        host="localhost",
        port=port,
        username="gateway",
        password_file=str(ROOT / "mqtt_gateway_password"),
        timeout=2.0,
    )
    gateway = GatewayConfig(id="plaintext_probe", topic_prefix="bosch/dev/state/plaintext_probe")
    availability = gateway.topic_prefix + "/availability"
    state = gateway.topic_prefix + "/state/point/1"
    async with aiomqtt.Client(
        "localhost",
        port,
        username="homeassistant",
        password=(ROOT / "mqtt_ha_password").read_text().strip(),
        protocol=aiomqtt.ProtocolVersion.V5,
        timeout=2,
    ) as observer:
        await observer.subscribe(availability, qos=1)
        rejected = False
        try:
            async with connect_publisher(config, gateway, password) as publisher:
                await publisher.publish(state, "synthetic-open")
                # Subscribe after publication to prove actual retained state delivery.
                await observer.subscribe(state, qos=1)
                await receive(observer, state, b"synthetic-open", retained=True)
                await publisher.publish(availability, "online")
                await receive(observer, availability, b"online")
                # Deliberately bypass topic guard only in this negative broker test.
                await publisher._client.publish("bosch/dev/command/area/probe", "IGNORED", qos=1)
        except PublishRejected:
            rejected = True
        assert rejected, "Broker must reject command publication"
        await receive(observer, availability, b"offline")
        await observer.unsubscribe(availability)
        await observer.subscribe(availability, qos=1)
        await receive(observer, availability, b"offline", retained=True)

    wrong_password_denied = False
    try:
        async with connect_publisher(config, gateway, "deliberately-wrong-synthetic-password"):
            raise AssertionError("Broker accepted wrong password")
    except aiomqtt.MqttError:
        wrong_password_denied = True
    assert wrong_password_denied

    # Omit transport entirely: default TLS must fail, never retry as plaintext.
    default_tls = MQTTConfig(
        host="localhost",
        port=port,
        username="gateway",
        password_file=str(ROOT / "mqtt_gateway_password"),
        timeout=2.0,
    )
    assert default_tls.transport == "tls"
    tls_failed = False
    try:
        async with asyncio.timeout(8):
            async with connect_publisher(default_tls, gateway, password):
                raise AssertionError("Default TLS silently downgraded to plaintext")
    except (aiomqtt.MqttError, OSError, TimeoutError):
        tls_failed = True
    assert tls_failed
    return {
        "explicit_plaintext_authenticated": True,
        "qos1_retained_state_delivered": True,
        "command_negative_puback_rejected": True,
        "offline_will_retained": True,
        "wrong_password_denied": True,
        "default_tls_to_plaintext_failed_no_downgrade": True,
        "broker_host_binding": "127.0.0.1:ephemeral",
        "lab_services_unchanged": True,
    }


async def main():
    os.umask(0o077)
    name = "bosch-plaintext-probe-" + uuid.uuid4().hex[:12]
    for filename in ("passwords", "acl", "mqtt_gateway_password", "mqtt_ha_password"):
        if not (ROOT / filename).is_file():
            raise RuntimeError("Run dev/bootstrap.py before the plaintext probe")
    with tempfile.TemporaryDirectory(prefix="bosch-plaintext-") as temporary:
        config = Path(temporary) / "mosquitto.conf"
        config.write_text(
            "listener 1883\nallow_anonymous false\n"
            "password_file /run/secrets/passwords\nacl_file /run/secrets/acl\n"
            "persistence false\nlog_dest stdout\n"
        )
        config.chmod(0o444)
        try:
            await docker(
                "run",
                "--detach",
                "--name",
                name,
                "--user",
                "10001:10001",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true",
                "--publish",
                "127.0.0.1::1883",
                "--mount",
                f"type=bind,src={config},dst=/mosquitto/config/mosquitto.conf,readonly",
                "--mount",
                f"type=bind,src={ROOT / 'passwords'},dst=/run/secrets/passwords,readonly",
                "--mount",
                f"type=bind,src={ROOT / 'acl'},dst=/run/secrets/acl,readonly",
                IMAGE,
            )
            binding = await docker("port", name, "1883/tcp")
            assert binding.startswith("127.0.0.1:") and "\n" not in binding
            port = int(binding.rsplit(":", 1)[1])
            await wait_ready(port)
            result = await probe(port)
        finally:
            # Always remove broker and its anonymous image volumes, including on failure.
            await docker("rm", "--force", "--volumes", name)
    result["temporary_broker_removed"] = True
    (ROOT / "plaintext-results.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
