"""MCP prompts served on Google-compatible mounts (currently Calendar only)."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP


def register_prompts(mcp: FastMCP, slug: str) -> None:
    """Register the prompts that belong to the connector mounted at ``/mcp/<slug>``."""
    if slug == "calendar":
        from gateway.mcp.calendar_prompts import register

        register(mcp)
