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

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from gateway.audit.log import shutdown_audit
from gateway.config import get_settings
from gateway.connectors.registry import BUNDLES, CONNECTORS
from gateway.db.engine import check_database
from gateway.logging import configure_logging
from gateway.mcp.context import IdentityMiddleware
from gateway.mcp.server import build_mcp, product_tool_filter
from gateway.storage.capella.client import close_client as close_capella


class _McpTrailingSlash:
    """Pure ASGI middleware: rewrite an exact MCP mount path to its trailing-slash form."""

    def __init__(self, app, paths: set[str]) -> None:
        self.app = app
        self.paths = paths

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket") and scope["path"] in self.paths:
            scope = dict(scope, path=scope["path"] + "/")
            if scope.get("raw_path"):
                scope["raw_path"] = scope["raw_path"] + b"/"
        await self.app(scope, receive, send)


def create_app() -> FastAPI:
    """Construct and return the FastAPI application."""
    configure_logging()
    settings = get_settings()
    from gateway.oauth.clerk import issuer_url

    issuer_url(settings)  # OAuth is mandatory; never fall back to legacy identity.

    mcp_servers = {"all": build_mcp(settings)}
    for connector in CONNECTORS:
        mcp_servers[connector.slug] = build_mcp(settings, product_tool_filter(connector.slug))
    for bundle in BUNDLES:
        mcp_servers[bundle.slug] = build_mcp(settings, product_tool_filter(bundle.slug))
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
            # Drain queued audit writes first: they use the shared Capella connection.
            shutdown_audit()
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
    app.state.access_specs = {spec.name: spec for spec in mcp_servers["all"].gateway_specs}
    app.state.access_catalog = [
        {
            "slug": c.slug,
            "name": c.display_name,
            "access_mode": c.access_mode.value,
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "risk": t.risk.value,
                    "default_tier": t.default_tier.value if t.default_tier else None,
                }
                for t in mcp_servers["all"].gateway_specs
                if c.matches(t)
            ],
        }
        for c in CONNECTORS
    ]

    from gateway.api.drive import router as drive_api_router
    from gateway.api.telegram import router as telegram_api_router
    from gateway.oauth.clerk_routes import router as clerk_oauth_router
    from gateway.oauth.routes import router as oauth_router

    app.include_router(oauth_router)
    app.include_router(clerk_oauth_router)
    app.include_router(drive_api_router)
    app.include_router(telegram_api_router)

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        """Landing page: the connections page, which sends signed-out users to login.

        Also the target of the Google OAuth callback redirect (``/?connected=google``).
        """
        return RedirectResponse("/connections", status_code=303)

    @app.get("/.well-known/apex-jwks.json", include_in_schema=False)
    async def apex_jwks() -> JSONResponse:
        """Public keys Apex uses to verify gateway-minted tokens (never private material)."""
        from gateway.oauth.downstream import SigningKeyError, jwks

        try:
            body = jwks(settings)
        except SigningKeyError:
            return JSONResponse({"error": "signing_key_not_configured"}, status_code=503)
        return JSONResponse(body, headers={"Cache-Control": "public, max-age=300"})

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

    connector_slugs = {connector.slug for connector in CONNECTORS} | {b.slug for b in BUNDLES}

    # Some MCP clients (e.g. Claude's connector probe) POST to the exact URL they were given and
    # never follow a redirect, so they would miss the 401 + resource_metadata challenge. Serve
    # /mcp and /mcp/{slug} directly by internally adding the trailing slash the mounts expect.
    app.add_middleware(_McpTrailingSlash, paths={"/mcp", *(f"/mcp/{s}" for s in connector_slugs)})

    @app.api_route("/mcp/{product}", methods=["GET", "POST", "DELETE"])
    async def redirect_unknown_product_mcp(product: str) -> RedirectResponse:
        """Unknown /mcp/{product} falls back to the all-tools endpoint."""
        return RedirectResponse("/mcp/", status_code=307)

    # Mount MCP Streamable HTTP apps behind identity enforcement: every request
    # must carry a trustworthy Open WebUI user identity. Connector-specific
    # mounts are registered before the backward-compatible /mcp catch-all.
    for slug in [*(c.slug for c in CONNECTORS), *(b.slug for b in BUNDLES)]:
        path = f"/mcp/{slug}"
        app.mount(path, IdentityMiddleware(mcp_apps[slug], settings, path))
    app.mount("/mcp", IdentityMiddleware(mcp_apps["all"], settings))

    return app
