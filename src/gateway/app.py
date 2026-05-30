"""FastAPI application factory.

Wires together the health endpoints, the Google OAuth routes, and the mounted
MCP Streamable HTTP app. The MCP session manager's lifespan MUST be entered from
the app lifespan or the Streamable HTTP transport raises "task group is not
initialized" at request time.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from gateway.config import get_settings
from gateway.db.engine import check_database
from gateway.logging import configure_logging
from gateway.mcp.context import IdentityMiddleware
from gateway.mcp.server import build_mcp


def create_app() -> FastAPI:
    """Construct and return the FastAPI application."""
    configure_logging()
    settings = get_settings()

    mcp = build_mcp(settings)
    # Accessing streamable_http_app() lazily creates the session manager.
    mcp_app = mcp.streamable_http_app()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="workspace-mcp-gateway", lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)

    from gateway.oauth.routes import router as oauth_router

    app.include_router(oauth_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness probe."""
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        """Readiness probe — verifies database connectivity."""
        if check_database():
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "not_ready"}, status_code=503)

    # Mount the MCP Streamable HTTP app behind identity enforcement: every
    # request must carry a trustworthy Open WebUI user identity.
    app.mount("/mcp", IdentityMiddleware(mcp_app, settings))

    return app
