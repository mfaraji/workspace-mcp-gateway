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

from gateway.config import Settings
from gateway.connectors.registry import CONNECTORS, by_slug
from gateway.providers.base import ToolSpec

ToolFilter = Callable[[ToolSpec], bool]


def build_mcp(settings: Settings, tool_filter: ToolFilter | None = None) -> FastMCP:
    """Build and return the configured FastMCP server."""
    mcp = FastMCP(
        name="workspace-mcp-gateway",
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
        connector.register(registry)

    registry.register_all(mcp, settings, predicate=tool_filter)
    return mcp


def product_tool_filter(product: str) -> ToolFilter:
    """Return a predicate for one connector's Open WebUI tool-server endpoint."""
    connector = by_slug(product)
    if connector is None:
        raise ValueError(f"unknown connector: {product}")

    def allow(spec: ToolSpec) -> bool:
        return spec.name == "system_get_current_time" or connector.matches(spec)

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
