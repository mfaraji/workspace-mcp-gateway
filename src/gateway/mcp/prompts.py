"""MCP prompts served on Google-compatible mounts (currently Calendar only)."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer


def register_prompts(mcp: MCPServer, slug: str) -> None:
    """Register the prompts that belong to the connector mounted at ``/mcp/<slug>``."""
    if slug == "calendar":
        from gateway.mcp.calendar_prompts import register

        register(mcp)
