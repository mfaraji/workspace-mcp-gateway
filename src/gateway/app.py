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
from gateway.connectors.registry import CONNECTORS
from gateway.db.engine import check_database
from gateway.logging import configure_logging
from gateway.mcp.context import IdentityMiddleware
from gateway.mcp.server import build_mcp, product_tool_filter
from gateway.storage.capella.client import close_client as close_capella


def create_app() -> FastAPI:
    """Construct and return the FastAPI application."""
    configure_logging()
    settings = get_settings()
    from gateway.oauth.clerk import issuer_url

    issuer_url(settings)  # OAuth is mandatory; never fall back to legacy identity.

    mcp_servers = {"all": build_mcp(settings)}
    for connector in CONNECTORS:
        mcp_servers[connector.slug] = build_mcp(settings, product_tool_filter(connector.slug))
    # Accessing streamable_http_app() lazily creates each session manager.
    mcp_apps = {name: mcp.streamable_http_app() for name, mcp in mcp_servers.items()}

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            async with AsyncExitStack() as stack:
                for mcp in mcp_servers.values():
                    await stack.enter_async_context(mcp.session_manager.run())
                yield
        finally:
            # Close the shared Capella connection if any tool opened it.
            close_capella()

    app = FastAPI(title="workspace-mcp-gateway", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        https_only=True,
        same_site="lax",
        max_age=3600,
    )
    from gateway.access.routes import router as access_router

    app.include_router(access_router)
    from gateway.access.connections import router as connections_page_router

    app.include_router(connections_page_router)
    from gateway.access.consent import router as consent_router

    app.include_router(consent_router)
    app.state.access_catalog = [
        {
            "slug": c.slug,
            "name": c.display_name,
            "access_mode": c.access_mode.value,
            "tools": [
                {"name": t.name, "description": t.description, "risk": t.risk.value}
                for t in mcp_servers["all"].gateway_specs
                if c.matches(t)
            ],
        }
        for c in CONNECTORS
    ]

    from gateway.api.drive import router as drive_api_router
    from gateway.api.telegram import router as telegram_api_router
    from gateway.connectors.routes import router as connectors_router
    from gateway.oauth.clerk_routes import router as clerk_oauth_router
    from gateway.oauth.routes import router as oauth_router

    app.include_router(oauth_router)
    app.include_router(clerk_oauth_router)
    app.include_router(drive_api_router)
    app.include_router(telegram_api_router)
    app.include_router(connectors_router)

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        """Landing page: the connections page, which sends signed-out users to login.

        Also the target of the Google OAuth callback redirect (``/?connected=google``).
        """
        return RedirectResponse("/connections", status_code=303)

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

    connector_slugs = {connector.slug for connector in CONNECTORS}

    @app.api_route("/mcp/{product}", methods=["GET", "POST", "DELETE"])
    async def redirect_product_mcp_root(product: str, request: Request) -> RedirectResponse:
        """Match /mcp/{product} behavior to /mcp by redirecting to the app root."""
        if product not in connector_slugs:
            return RedirectResponse("/mcp/", status_code=307)
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"/mcp/{product}/{query}", status_code=307)

    # Mount MCP Streamable HTTP apps behind identity enforcement: every request
    # must carry a trustworthy Open WebUI user identity. Connector-specific
    # mounts are registered before the backward-compatible /mcp catch-all.
    for connector in CONNECTORS:
        path = f"/mcp/{connector.slug}"
        app.mount(path, IdentityMiddleware(mcp_apps[connector.slug], settings, path))
    app.mount("/mcp", IdentityMiddleware(mcp_apps["all"], settings))

    return app
