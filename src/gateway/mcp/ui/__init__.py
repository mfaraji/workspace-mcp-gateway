"""MCP Apps widgets: self-contained HTML documents served as ``ui://`` resources.

A tool opts in with ``ToolSpec.ui_resource``; the matching widget is registered here.
Widgets are plain HTML with the shared stylesheet and bridge inlined at load time, because
the host sandbox forbids network access (no CDN, no fetch). See ``docs/mcp-apps.md``.
"""

from __future__ import annotations

from functools import cache
from importlib import resources

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.resources import FunctionResource
from pydantic import AnyUrl

MIME_TYPE = "text/html;profile=mcp-app"

# ui:// URI -> (widget file, human title)
WIDGETS: dict[str, tuple[str, str]] = {
    "ui://tasks/list": ("tasks.html", "Google Tasks"),
    "ui://calendar/agenda": ("calendar.html", "Google Calendar"),
    "ui://drive/files": ("drive.html", "Google Drive"),
    "ui://gmail/mail": ("gmail.html", "Gmail"),
    "ui://telegram/send": ("telegram.html", "Telegram"),
    "ui://finance/rates": ("finance.html", "Exchange rates"),
    "ui://recipes/book": ("recipes.html", "Recipes"),
    "ui://apex/billing": ("apex.html", "Apex billing"),
    "ui://wealth/overview": ("wealth.html", "Apex Wealth"),
}

# Empty CSP: widgets need no network, so the host applies its default-deny policy.
_RESOURCE_META = {"ui": {"csp": {}, "prefersBorder": False}}


def _read(*parts: str) -> str:
    return resources.files(__package__).joinpath(*parts).read_text(encoding="utf-8")


@cache
def render_widget(uri: str) -> str:
    """Return the full HTML for a widget with the shared CSS and bridge inlined."""
    filename, _ = WIDGETS[uri]
    html = _read("widgets", filename)
    return html.replace("{{SHARED_CSS}}", _read("shared", "base.css")).replace(
        "{{SHARED_JS}}", _read("shared", "bridge.js")
    )


def register_ui(mcp: FastMCP, uris: set[str]) -> None:
    """Add one resource per widget that a registered tool references."""
    for uri in sorted(uris):
        if uri not in WIDGETS:
            raise ValueError(f"tool references unknown widget {uri}")
        _, title = WIDGETS[uri]
        mcp.add_resource(
            FunctionResource(
                uri=AnyUrl(uri),
                name=uri.removeprefix("ui://").replace("/", "-"),
                title=title,
                description=f"Interactive widget: {title}",
                mime_type=MIME_TYPE,
                fn=lambda uri=uri: render_widget(uri),
                meta=_RESOURCE_META,
            )
        )
