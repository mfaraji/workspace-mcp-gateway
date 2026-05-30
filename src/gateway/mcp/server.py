"""MCP server construction.

``build_mcp`` creates the ``FastMCP`` instance, collects tools from the provider
modules into a :class:`~gateway.providers.registry.ToolRegistry`, and registers
them. Providers self-register via their ``register(registry)`` entrypoint, so
adding or removing a provider is a one-line change here.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from gateway.config import Settings


def build_mcp(settings: Settings) -> FastMCP:
    """Build and return the configured FastMCP server."""
    mcp = FastMCP(name="workspace-mcp-gateway", stateless_http=True, streamable_http_path="/")

    # Local import to avoid a circular import at module load.
    from gateway.providers.registry import ToolRegistry

    registry = ToolRegistry()

    # Provider self-registration. Each provider module exposes register(registry).
    from gateway.providers.google.calendar import read as google_calendar_read
    from gateway.providers.google.calendar import write as google_calendar_write

    google_calendar_read.register(registry)
    google_calendar_write.register(registry)

    registry.register_all(mcp, settings)
    return mcp


def build_mcp_skeleton(settings: Settings) -> FastMCP:
    """Build a FastMCP with no tools registered (used until providers land)."""
    return FastMCP(name="workspace-mcp-gateway", stateless_http=True, streamable_http_path="/")
