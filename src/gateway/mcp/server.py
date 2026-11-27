"""MCP server construction.

``build_mcp`` creates the ``MCPServer`` instance, collects tools from the
registered connectors into a :class:`~gateway.providers.registry.ToolRegistry`,
and registers them. Connectors self-register via their ``register(registry)``
entrypoint, so adding or removing one is a single entry in
:data:`gateway.connectors.registry.CONNECTORS`.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Tool as MCPTool

from gateway.config import Settings
from gateway.connectors.base import AccessMode, Connector
from gateway.connectors.registry import CONNECTORS, by_slug, connector_slugs_for
from gateway.providers.base import RiskLevel, ToolSpec

ToolFilter = Callable[[ToolSpec], bool]


class GatewayMCPServer(MCPServer):
    """Advertise only the tools granted to this OAuth user and application."""

    def __init__(
        self,
        *args,
        oauth_enabled: bool = False,
        transport_security: TransportSecuritySettings | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.oauth_enabled = oauth_enabled
        self._transport_security = transport_security

    def streamable_http_app(self, **kwargs):
        """Stateless, mounted at its own root; mcp 2.x takes these per app, not per server."""
        kwargs.setdefault("stateless_http", True)
        kwargs.setdefault("streamable_http_path", "/")
        kwargs.setdefault("transport_security", self._transport_security)
        return super().streamable_http_app(**kwargs)

    async def list_tools(self) -> list[MCPTool]:
        tools = await super().list_tools()
        import anyio

        from gateway.access.policy import (
            allowed_tools,
            setup_url,
            tool_allowed,
            upstream_permits,
        )
        from gateway.db.engine import session_scope
        from gateway.identity.resolver import get_or_create_user
        from gateway.mcp.context import require_current_user

        auth = require_current_user()

        def permitted():
            with session_scope() as session:
                user = get_or_create_user(session, auth)
                return allowed_tools(session, user.id, self.gateway_settings, auth)

        names = await anyio.to_thread.run_sync(permitted)
        specs = self.gateway_exposed  # exposed (possibly bare) tool name -> spec
        granted = [t for t in tools if tool_allowed(specs[t.name], auth, names)]

        def upstream_filtered():
            # May call the upstream (Apex workspaces, cached ~60s), so off the event loop.
            return [
                t for t in granted if upstream_permits(self.gateway_settings, auth, specs[t.name])
            ]

        tools = [t.model_copy(deep=True) for t in await anyio.to_thread.run_sync(upstream_filtered)]
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
            # mcp 2.x drops unknown top-level tool fields, so the scheme rides in ``_meta``
            # (the Apps SDK reads ``_meta.securitySchemes`` as well as the top-level field).
            for tool in tools:
                tool.meta = {
                    **(tool.meta or {}),
                    "securitySchemes": [{"type": "oauth2", "scopes": ["mcp:tools"]}],
                }
        return tools

    async def _granted_ui_uris(self) -> set[str]:
        """Widgets are served only when a tool the caller may use references them."""
        import anyio

        if not self.gateway_settings.mcp_apps_enabled or self.gateway_bare:
            return set()

        from gateway.access.policy import allowed_tools, tool_allowed, upstream_permits
        from gateway.db.engine import session_scope
        from gateway.identity.resolver import get_or_create_user
        from gateway.mcp.context import require_current_user

        auth = require_current_user()

        def permitted():
            with session_scope() as session:
                user = get_or_create_user(session, auth)
                return allowed_tools(session, user.id, self.gateway_settings, auth)

        names = await anyio.to_thread.run_sync(permitted)
        candidates = [
            spec
            for spec in self.gateway_specs
            if spec.ui_resource and self.gateway_filter(spec) and tool_allowed(spec, auth, names)
        ]

        def upstream_filtered():
            return {
                spec.ui_resource
                for spec in candidates
                if upstream_permits(self.gateway_settings, auth, spec)
            }

        return await anyio.to_thread.run_sync(upstream_filtered)

    async def list_resources(self):
        uris = await self._granted_ui_uris()
        return [r for r in await super().list_resources() if str(r.uri) in uris]

    async def read_resource(self, uri, context=None):
        if str(uri) not in await self._granted_ui_uris():
            raise ResourceNotFoundError(f"Unknown resource: {uri}")
        return await super().read_resource(uri, context)


def build_mcp(
    settings: Settings,
    tool_filter: ToolFilter | None = None,
    connector: Connector | None = None,
) -> MCPServer:
    """Build and return the configured MCP server.

    With a ``connector`` that defines ``google_names`` this is that connector's own
    mount: it exposes exactly Google's tool set under Google's bare names (and the
    connector's prompts) instead of the gateway's prefixed tools and widgets.
    """
    mcp = GatewayMCPServer(
        name="workspace-mcp-gateway",
        oauth_enabled=bool(settings.clerk_issuer),
        transport_security=_transport_security(settings),
    )

    # Local import to avoid a circular import at module load.
    from gateway.providers.registry import ToolRegistry
    from gateway.providers.system import time as system_time

    registry = ToolRegistry()

    system_time.register(registry)
    for each in CONNECTORS:
        each.register(registry, settings)

    for each in CONNECTORS:
        if each.access_mode is AccessMode.AUTHENTICATED_SHARED and any(
            each.matches(spec) and spec.risk is not RiskLevel.READ for spec in registry._specs
        ):
            raise ValueError(f"Shared connector {each.slug} must contain only read tools")

    mcp.gateway_filter = tool_filter or (lambda _: True)
    mcp.gateway_settings = settings
    mcp.gateway_specs = registry._specs
    names = (
        dict(connector.google_names) if connector is not None and connector.google_names else None
    )
    mcp.gateway_bare = names is not None
    registry.register_all(mcp, settings, predicate=tool_filter, names=names)
    mcp.gateway_exposed = {
        (names.get(spec.name, spec.name) if names is not None else spec.name): spec
        for spec in registry._specs
        if mcp.gateway_filter(spec)
        and (names is None or spec.name in names or spec.name == "system_get_current_time")
    }
    if names is not None:
        from gateway.mcp.prompts import register_prompts

        register_prompts(mcp, connector.slug)

    if settings.mcp_apps_enabled and not mcp.gateway_bare:
        from gateway.mcp.ui import register_ui

        register_ui(
            mcp,
            {
                spec.ui_resource
                for spec in registry._specs
                if spec.ui_resource and mcp.gateway_filter(spec)
            },
        )
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
