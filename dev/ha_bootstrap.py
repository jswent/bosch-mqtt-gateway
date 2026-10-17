"""Onboard a real disposable HA via supported HTTP APIs; never write .storage."""

import asyncio
import json
import os
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent / "generated"
BASE = "http://127.0.0.1:18123"


async def main():
    os.umask(0o077)
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as client:
        for _ in range(120):
            try:
                status = await client.get("/api/onboarding")
                if status.status_code in (200, 401, 404):
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        token_path = ROOT / "ha_tokens.json"
        if token_path.exists():
            saved = json.loads(token_path.read_text())
            response = await client.post(
                "/auth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": saved["refresh_token"],
                    "client_id": BASE + "/",
                },
            )
            response.raise_for_status()
            token = response.json()["access_token"]
        else:
            response = await client.post(
                "/api/onboarding/users",
                json={
                    "name": "Development",
                    "username": "developer",
                    "password": (ROOT / "ha_password").read_text(),
                    "client_id": BASE + "/",
                    "language": "en",
                },
            )
            response.raise_for_status()
            response = await client.post(
                "/auth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": response.json()["auth_code"],
                    "client_id": BASE + "/",
                },
            )
            response.raise_for_status()
            saved = response.json()
            token_path.write_text(json.dumps(saved))
            token = saved["access_token"]
        (ROOT / "ha_access_token").write_text(token)
        client.headers["Authorization"] = "Bearer " + token
        for step, body in [
            ("core_config", {}),
            ("analytics", {}),
            ("integration", {"client_id": BASE + "/", "redirect_uri": BASE + "/"}),
        ]:
            response = await client.post("/api/onboarding/" + step, json=body)
            if response.status_code not in (200, 403, 404):
                response.raise_for_status()
        response = await client.get("/api/config/config_entries/entry")
        response.raise_for_status()
        if any(entry["domain"] == "mqtt" for entry in response.json()):
            print("HA already has MQTT configured; refreshed test token.")
            return
        response = await client.post(
            "/api/config/config_entries/flow",
            json={"handler": "mqtt", "show_advanced_options": True},
        )
        response.raise_for_status()
        flow = response.json()
        response = await client.post(
            "/api/file_upload",
            files={"file": ("ca.crt", (ROOT / "ca.crt").read_bytes(), "application/x-pem-file")},
        )
        response.raise_for_status()
        file_id = response.json()["file_id"]
        response = await client.post(
            "/api/config/config_entries/flow/" + flow["flow_id"],
            json={
                "broker": "mqtt",
                "port": 8883,
                "username": "homeassistant",
                "password": (ROOT / "mqtt_ha_password").read_text(),
                "protocol": "5",
                "other_settings": {
                    "set_ca_cert": "custom",
                    "certificate": file_id,
                    "set_client_cert": False,
                    "tls_insecure": False,
                    "transport": "tcp",
                },
            },
        )
        response.raise_for_status()
        result = response.json()
        if result["type"] != "create_entry":
            raise RuntimeError("MQTT setup failed: " + str(result.get("errors", result["type"])))
        print("Real Home Assistant onboarded with verified TLS MQTT via config-flow API.")


if __name__ == "__main__":
    asyncio.run(main())
