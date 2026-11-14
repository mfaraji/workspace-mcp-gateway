"""MCP server construction.

``build_mcp`` creates the ``FastMCP`` instance, collects tools from the
registered connectors into a :class:`~gateway.providers.registry.ToolRegistry`,
and registers them. Connectors self-register via their ``register(registry)``
entrypoint, so adding or removing one is a single entry in
:data:`gateway.connectors.registry.CONNECTORS`.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Tool as MCPTool

from gateway.config import Settings
from gateway.connectors.base import AccessMode
from gateway.connectors.registry import CONNECTORS, by_slug, connector_slugs_for
from gateway.providers.base import RiskLevel, ToolSpec

ToolFilter = Callable[[ToolSpec], bool]


class GatewayFastMCP(FastMCP):
    """Advertise only the tools granted to this OAuth user and application."""

    def __init__(self, *args, oauth_enabled: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.oauth_enabled = oauth_enabled

    async def list_tools(self) -> list[MCPTool]:
        tools = await super().list_tools()
        import anyio

        from gateway.access.policy import allowed_tools, setup_url, tool_allowed
        from gateway.db.engine import session_scope
        from gateway.identity.resolver import get_or_create_user
        from gateway.mcp.context import require_current_user

        auth = require_current_user()

        def permitted():
            with session_scope() as session:
                user = get_or_create_user(session, auth)
                return allowed_tools(session, user.id, self.gateway_settings, auth)

        names = await anyio.to_thread.run_sync(permitted)
        specs = {spec.name: spec for spec in self.gateway_specs}
        tools = [
            t.model_copy(deep=True)
            for t in tools
            if tool_allowed(specs[t.name], auth, names)
        ]
        has_personal = any(
            c.access_mode is AccessMode.PERSONAL
            and any(c.matches(spec) and self.gateway_filter(spec) for spec in self.gateway_specs)
            for c in CONNECTORS
        )
        for tool in tools:
            if tool.name == "system_get_current_time" and has_personal:
                tool.description += " Manage this client's connector permissions: " + setup_url(
                    self.gateway_settings, auth
                )
        if self.oauth_enabled:
            for tool in tools:
                tool.__pydantic_extra__["securitySchemes"] = [
                    {"type": "oauth2", "scopes": ["mcp:tools"]}
                ]
        return tools


def build_mcp(settings: Settings, tool_filter: ToolFilter | None = None) -> FastMCP:
    """Build and return the configured FastMCP server."""
    mcp = GatewayFastMCP(
        name="workspace-mcp-gateway",
        oauth_enabled=bool(settings.clerk_issuer),
        stateless_http=True,
        streamable_http_path="/",
        transport_security=_transport_security(settings),
    )

    # Local import to avoid a circular import at module load.
    from gateway.providers.registry import ToolRegistry
    from gateway.providers.system import time as system_time

    registry = ToolRegistry()

    system_time.register(registry)
    for connector in CONNECTORS:
        connector.register(registry, settings)

    for connector in CONNECTORS:
        if connector.access_mode is AccessMode.AUTHENTICATED_SHARED and any(
            connector.matches(spec) and spec.risk is not RiskLevel.READ
            for spec in registry._specs
        ):
            raise ValueError(f"Shared connector {connector.slug} must contain only read tools")

    mcp.gateway_filter = tool_filter or (lambda _: True)
    mcp.gateway_settings = settings
    mcp.gateway_specs = registry._specs
    registry.register_all(mcp, settings, predicate=tool_filter)
    return mcp


def product_tool_filter(product: str) -> ToolFilter:
    """Return a predicate for one connector's (or bundle's) tool-server endpoint."""
    slugs = connector_slugs_for(product)
    if slugs is None:
        raise ValueError(f"unknown connector: {product}")
    members = [c for c in (by_slug(slug) for slug in sorted(slugs)) if c is not None]

    def allow(spec: ToolSpec) -> bool:
        return spec.name == "system_get_current_time" or any(c.matches(spec) for c in members)

    return allow


def build_mcp_skeleton(settings: Settings) -> FastMCP:
    """Build a FastMCP with no tools registered (used until providers land)."""
    return FastMCP(
        name="workspace-mcp-gateway",
        stateless_http=True,
        streamable_http_path="/",
        transport_security=_transport_security(settings),
    )


def _transport_security(settings: Settings) -> TransportSecuritySettings:
    """Allow MCP requests from configured public/trusted gateway origins."""
    origins = {
        settings.base_url.rstrip("/"),
        settings.trusted_open_webui_origin.rstrip("/"),
    }
    hosts = {
        "127.0.0.1",
        "127.0.0.1:8000",
        "127.0.0.1:8001",
        "localhost",
        "localhost:8000",
        "localhost:8001",
        "0.0.0.0",
        "0.0.0.0:8000",
        "0.0.0.0:8001",
    }
    for origin in origins:
        parsed = urlparse(origin)
        if parsed.netloc:
            hosts.add(parsed.netloc)

    return TransportSecuritySettings(
        allowed_origins=sorted(origins),
        allowed_hosts=sorted(hosts),
    )
