"""FastAPI application factory.

Wires together the health endpoints, the Google OAuth routes, and the mounted
MCP Streamable HTTP app. The MCP session manager's lifespan MUST be entered from
the app lifespan or the Streamable HTTP transport raises "task group is not
initialized" at request time.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from gateway.config import get_settings
from gateway.db.engine import check_database
from gateway.logging import configure_logging
from gateway.mcp.context import IdentityMiddleware
from gateway.mcp.server import build_mcp, product_tool_filter


def create_app() -> FastAPI:
    """Construct and return the FastAPI application."""
    configure_logging()
    settings = get_settings()

    mcp_servers = {
        "all": build_mcp(settings),
        "calendar": build_mcp(settings, product_tool_filter("calendar")),
        "drive": build_mcp(settings, product_tool_filter("drive")),
        "tasks": build_mcp(settings, product_tool_filter("tasks")),
    }
    # Accessing streamable_http_app() lazily creates each session manager.
    mcp_apps = {name: mcp.streamable_http_app() for name, mcp in mcp_servers.items()}

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for mcp in mcp_servers.values():
                await stack.enter_async_context(mcp.session_manager.run())
            yield

    app = FastAPI(title="workspace-mcp-gateway", lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)

    from gateway.oauth.routes import router as oauth_router

    app.include_router(oauth_router)

    @app.get("/")
    async def index(connected: str | None = None) -> JSONResponse:
        """Small browser landing page for OAuth redirects."""
        if connected:
            return JSONResponse({"status": "connected", "provider": connected})
        return JSONResponse({"status": "ok", "service": "workspace-mcp-gateway"})

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

    @app.api_route("/mcp/{product}", methods=["GET", "POST", "DELETE"])
    async def redirect_product_mcp_root(product: str, request: Request) -> RedirectResponse:
        """Match /mcp/{product} behavior to /mcp by redirecting to the app root."""
        if product not in {"calendar", "drive", "tasks"}:
            return RedirectResponse("/mcp/", status_code=307)
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"/mcp/{product}/{query}", status_code=307)

    # Mount MCP Streamable HTTP apps behind identity enforcement: every request
    # must carry a trustworthy Open WebUI user identity. Product-specific mounts
    # are registered before the backward-compatible /mcp catch-all.
    app.mount("/mcp/calendar", IdentityMiddleware(mcp_apps["calendar"], settings))
    app.mount("/mcp/drive", IdentityMiddleware(mcp_apps["drive"], settings))
    app.mount("/mcp/tasks", IdentityMiddleware(mcp_apps["tasks"], settings))
    app.mount("/mcp", IdentityMiddleware(mcp_apps["all"], settings))

    return app
