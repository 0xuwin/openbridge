"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI

from openbridge.config import Config
from openbridge.server.routes import router
from openbridge.store import Store


def create_app(cfg: Config, store: Store) -> FastAPI:
    """Build and return the configured FastAPI application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Single shared client for all upstream requests; reuses the connection
        # pool and TLS sessions across requests.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(120, connect=10),
        ) as client:
            app.state.http_client = client
            yield

    app = FastAPI(
        title="OpenBridge",
        description="Bridge ChatGPT Pro/Plus subscriptions to an OpenAI-compatible API",
        version="0.1.0",
        docs_url=None,       # disable Swagger UI in production
        redoc_url=None,
        lifespan=lifespan,
    )

    # Attach shared state so routes/middleware can access them
    app.state.config = cfg
    app.state.store = store

    app.include_router(router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        has_tokens = store.get_oauth() is not None
        return {
            "status": "ok",
            "authenticated": "yes" if has_tokens else "no",
        }

    return app
