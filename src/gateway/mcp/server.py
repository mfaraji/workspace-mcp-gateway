"""MCP server construction.

``build_mcp`` creates the ``FastMCP`` instance, collects tools from the provider
modules into a :class:`~gateway.providers.registry.ToolRegistry`, and registers
them. Providers self-register via their ``register(registry)`` entrypoint, so
adding or removing a provider is a one-line change here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from gateway.config import Settings
from gateway.providers.base import ToolSpec

ProductEndpoint = Literal["calendar", "drive", "tasks"]
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

    registry = ToolRegistry()

    # Provider self-registration. Each provider module exposes register(registry).
    from gateway.providers.google.calendar import read as google_calendar_read
    from gateway.providers.google.calendar import write as google_calendar_write
    from gateway.providers.system import time as system_time

    system_time.register(registry)
    google_calendar_read.register(registry)
    google_calendar_write.register(registry)

    registry.register_all(mcp, settings, predicate=tool_filter)
    return mcp


def product_tool_filter(product: ProductEndpoint) -> ToolFilter:
    """Return a predicate for product-specific Open WebUI tool-server endpoints."""
    product_prefixes = {
        "calendar": "google_calendar_",
        "drive": "google_drive_",
        "tasks": "google_tasks_",
    }
    prefix = product_prefixes[product]

    def allow(spec: ToolSpec) -> bool:
        return spec.name == "system_get_current_time" or spec.name.startswith(prefix)

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
        "localhost",
        "localhost:8000",
        "0.0.0.0",
        "0.0.0.0:8000",
    }
    for origin in origins:
        parsed = urlparse(origin)
        if parsed.netloc:
            hosts.add(parsed.netloc)

    return TransportSecuritySettings(
        allowed_origins=sorted(origins),
        allowed_hosts=sorted(hosts),
    )
