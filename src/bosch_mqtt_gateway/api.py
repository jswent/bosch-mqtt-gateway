"""Read-only diagnostics. No command or configuration routes exist in v1."""

from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .runtime import Gateway


def create_app(gateway: Gateway, *, manage_lifecycle: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not manage_lifecycle:
            yield
            return
        task = asyncio.create_task(gateway.run(), name="gateway-supervisor")
        try:
            yield
        finally:
            gateway.request_stop()
            try:
                async with asyncio.timeout(20):
                    await task
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(
        title="Bosch MQTT Gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    bearer = HTTPBearer(auto_error=False)
    bearer_dependency = Depends(bearer)

    async def authorize(
        credentials: HTTPAuthorizationCredentials | None = bearer_dependency,
    ) -> None:
        token = gateway.secrets.debug_token
        if token is None:
            raise HTTPException(status_code=503, detail="diagnostics not configured")
        if credentials is None or not secrets.compare_digest(
            credentials.credentials.encode("utf-8"), token.encode("utf-8")
        ):
            raise HTTPException(
                status_code=401,
                detail="authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        return response

    @app.get("/healthz")
    async def health():
        return {"status": "alive"}

    @app.get("/readyz")
    async def ready():
        return JSONResponse({"ready": gateway.ready}, status_code=200 if gateway.ready else 503)

    @app.get("/status", dependencies=[Depends(authorize)])
    async def status():
        return gateway.status()

    @app.get("/metrics", dependencies=[Depends(authorize)])
    async def metrics():
        return gateway.metrics.snapshot()

    @app.get("/debug/state", dependencies=[Depends(authorize)])
    async def state():
        return {"ready": gateway.ready, "entities": gateway.debug_states()}

    return app
