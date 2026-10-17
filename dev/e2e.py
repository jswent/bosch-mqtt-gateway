"""Real broker + wire simulator + authenticated HA lifecycle and latency checks."""

import asyncio
import json
import os
import shutil
import ssl
import statistics
import time
from datetime import datetime
from pathlib import Path

import httpx
import paho.mqtt.client as mqtt
import websockets

ROOT = Path(__file__).resolve().parent / "generated"
DOCKER = shutil.which("docker") or "/usr/local/bin/docker"


async def compose(*args):
    p = await asyncio.create_subprocess_exec(
        DOCKER, "compose", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, error = await p.communicate()
    if p.returncode:
        raise RuntimeError(error.decode())


async def wait_for(check, timeout=90):
    async with asyncio.timeout(timeout):
        while True:
            try:
                value = await check()
                if value:
                    return value
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)


def acl_check():
    results = {}
    for user, secret in [
        ("gateway", "mqtt_gateway_password"),
        ("homeassistant", "mqtt_ha_password"),
    ]:
        codes = []
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
        client.username_pw_set(user, (ROOT / secret).read_text())
        client.tls_set_context(ssl.create_default_context(cafile=str(ROOT / "ca.crt")))
        client.on_publish = lambda c, u, mid, reason, props, codes=codes: codes.append(reason.value)
        client.connect("localhost", 18883)
        client.loop_start()
        info = client.publish("bosch/dev/command/area/1", "ARM_AWAY", qos=1)
        info.wait_for_publish(timeout=10)
        client.disconnect()
        client.loop_stop()
        assert codes == [135], (user, codes)
        results[user + "_command_denied"] = True
    # ACL may allow SUBACK yet filter delivery; test read denial using retained online state.
    messages = []
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
    client.username_pw_set("gateway", (ROOT / "mqtt_gateway_password").read_text())
    client.tls_set_context(ssl.create_default_context(cafile=str(ROOT / "ca.crt")))
    client.on_message = lambda c, u, m: messages.append(m.topic)
    client.connect("localhost", 18883)
    client.subscribe("bosch/dev/#")
    client.loop_start()
    time.sleep(1)
    client.disconnect()
    client.loop_stop()
    assert not messages
    results["gateway_no_read_delivery"] = True
    return results


async def main():
    token = (ROOT / "ha_access_token").read_text()
    results = {}
    async with (
        httpx.AsyncClient(
            base_url="http://127.0.0.1:18123",
            headers={"Authorization": "Bearer " + token},
            timeout=15,
        ) as ha,
        httpx.AsyncClient(base_url="http://127.0.0.1:18081", timeout=15) as sim,
        httpx.AsyncClient(base_url="http://127.0.0.1:18080") as gateway,
    ):

        async def ready():
            return (await gateway.get("/readyz")).status_code == 200

        await wait_for(ready)

        async def all_points():
            states = (await ha.get("/api/states")).json()
            points = [s for s in states if s["entity_id"].startswith("binary_sensor.point")]
            return (
                points
                if len(points) == 600 and all(s["state"] in ("on", "off") for s in points)
                else None
            )

        await wait_for(all_points)
        results["discovered_points"] = 600
        results.update(await asyncio.to_thread(acl_check))
        async with websockets.connect("ws://127.0.0.1:18123/api/websocket") as ws:
            assert json.loads(await ws.recv())["type"] == "auth_required"
            await ws.send(json.dumps({"type": "auth", "access_token": token}))
            assert json.loads(await ws.recv())["type"] == "auth_ok"
            await ws.send(
                json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"})
            )
            assert json.loads(await ws.recv())["success"]
            latency, receive_latency = [], []
            # Unique point each sample; alternate against current state so every update changes HA.
            for point in range(1, 101):
                entity = f"binary_sensor.point{point}"
                current = (await ha.get("/api/states/" + entity)).json()["state"]
                status = 3 if current == "on" else 2
                expected = "off" if status == 3 else "on"
                before = time.time_ns()
                emitted = (
                    await sim.post("/event", json={"point": point, "status": status})
                ).json()["emitted_ns"]
                async with asyncio.timeout(10):
                    while True:
                        message = json.loads(await ws.recv())
                        event = message.get("event", {})
                        data = event.get("data", {})
                        state = data.get("new_state") or {}
                        if data.get("entity_id") == entity and state.get("state") == expected:
                            timestamp = (
                                datetime.fromisoformat(state["last_updated"]).timestamp() * 1e9
                            )
                            latency.append((timestamp - emitted) / 1e6)
                            receive_latency.append((time.time_ns() - before) / 1e6)
                            break
            results["latency_ms"] = {
                "samples": len(latency),
                "p50": statistics.median(latency),
                "p95": sorted(latency)[94],
                "max": max(latency),
                "client_receive_p50": statistics.median(receive_latency),
            }
        # Actual HA disarm service exercises the inert broker command path.
        response = await ha.post(
            "/api/services/alarm_control_panel/alarm_disarm",
            json={"entity_id": "alarm_control_panel.area1"},
        )
        assert response.status_code == 200
        results["ha_alarm_service_http_status"] = response.status_code
        await asyncio.sleep(0.5)
        assert (await ha.get("/api/states/alarm_control_panel.area1")).json()["state"] == "disarmed"
        commands = (await sim.get("/health")).json()["commands"]
        assert not set(commands).intersection({0x27, 0x2D, 0x32, 0x11, 0x07})
        results["ha_alarm_service_inert"] = True

        async def unavailable():
            return (await ha.get("/api/states/binary_sensor.point1")).json()[
                "state"
            ] == "unavailable"

        for fault in ["disconnect", "malformed"]:
            await sim.post("/" + fault)
            await wait_for(unavailable, 20)
            await wait_for(ready)
            await wait_for(all_points)
            results[fault + "_recovered"] = True
        # Freeze reconnect long enough to observe broker Last Will after process kill.
        await compose("stop", "gateway")
        await wait_for(unavailable)
        await compose("start", "gateway")
        await wait_for(ready)
        await wait_for(all_points)
        results["graceful_shutdown_recovered"] = True
        await compose("kill", "-s", "SIGKILL", "gateway")
        await wait_for(unavailable, 30)
        await compose("start", "gateway")
        await wait_for(ready)
        await wait_for(all_points)
        results["crash_will_recovered"] = True
        await compose("restart", "mqtt")
        await wait_for(ready)
        await wait_for(all_points)
        results["broker_restart_recovered"] = True
        # HA restart validates retained discovery and state without new sensor injection.
        await compose("restart", "homeassistant")
        await wait_for(all_points, 120)
        results["ha_restart_retained_recovered"] = True
    os.umask(0o077)
    (ROOT / "e2e-results.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
