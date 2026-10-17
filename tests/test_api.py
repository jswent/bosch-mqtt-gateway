import pytest
from httpx import ASGITransport, AsyncClient

from bosch_mqtt_gateway.api import create_app
from bosch_mqtt_gateway.runtime import Gateway, Secrets


@pytest.fixture
def gateway(config):
    return Gateway(config, Secrets("panel-secret", None, "mqtt-secret", "d" * 48))


async def test_health_and_readiness_expose_no_details(gateway):
    async with AsyncClient(
        transport=ASGITransport(app=create_app(gateway, manage_lifecycle=False)),
        base_url="http://test",
    ) as client:
        response = await client.get("/healthz")
        assert response.json() == {"status": "alive"}
        assert response.headers["cache-control"] == "no-store"
        response = await client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"ready": False}
        gateway.ready = True
        assert (await client.get("/readyz")).status_code == 200


@pytest.mark.parametrize("path", ["/status", "/metrics", "/debug/state"])
async def test_diagnostics_require_bearer(gateway, path):
    async with AsyncClient(
        transport=ASGITransport(app=create_app(gateway, manage_lifecycle=False)),
        base_url="http://test",
    ) as client:
        assert (await client.get(path)).status_code == 401
        assert (
            await client.get(path, headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        response = await client.get(path, headers={"Authorization": "Bearer " + "d" * 48})
        assert response.status_code == 200
        assert "panel-secret" not in response.text
        assert "mqtt-secret" not in response.text


@pytest.mark.parametrize(
    "path", ["/commands", "/arm", "/config", "/docs", "/openapi.json", "/status"]
)
async def test_no_mutation_or_api_docs(gateway, path):
    async with AsyncClient(
        transport=ASGITransport(app=create_app(gateway, manage_lifecycle=False)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            path, json={"action": "arm_away"}, headers={"Authorization": "Bearer " + "d" * 48}
        )
        assert response.status_code in (404, 405)


async def test_missing_diagnostic_token_disables_access(config):
    gateway = Gateway(config, Secrets("panel-secret", None, "mqtt-secret", None))
    async with AsyncClient(
        transport=ASGITransport(app=create_app(gateway, manage_lifecycle=False)),
        base_url="http://test",
    ) as client:
        assert (await client.get("/status")).status_code == 503


async def test_non_ascii_bearer_is_rejected_cleanly(gateway):
    async with AsyncClient(
        transport=ASGITransport(app=create_app(gateway, manage_lifecycle=False)),
        base_url="http://test",
    ) as client:
        response = await client.get("/status", headers={b"Authorization": b"Bearer \xc3\xa9"})
        assert response.status_code == 401


async def test_api_lifespan_starts_and_stops_supervisor(gateway):
    from unittest.mock import AsyncMock

    gateway.run = AsyncMock()
    app = create_app(gateway)
    async with app.router.lifespan_context(app):
        pass
    gateway.run.assert_awaited_once()
    assert gateway._stop.is_set()
