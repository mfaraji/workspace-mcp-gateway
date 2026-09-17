"""The ``Connector`` descriptor: one independently-connectable group of MCP tools.

Each connector owns its own ``/mcp/<slug>`` endpoint and tool-name prefix, so a
user (or an MCP client like Claude, Cursor, or ChatGPT) can enable it on its
own without pulling in every other provider. ``upstream_provider`` is a
separate concept: it is the key under which upstream credentials are stored
(:class:`gateway.db.models.ProviderConnection`), and multiple connectors may
share one — the three Google connectors all store credentials under
``upstream_provider="google"`` so one Google account covers all of them, with
scopes unioned incrementally as each is enabled.

Adding a new connector is one entry in
:data:`gateway.connectors.registry.CONNECTORS` plus its provider package(s)
with a ``register(registry)`` entrypoint.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gateway.config import Settings
    from gateway.connectors.upstream import UpstreamAuthStrategy
    from gateway.providers.base import ToolSpec
    from gateway.providers.registry import ToolRegistry

# A provider-raised exception -> a stable, non-sensitive error code, or None
# if this classifier doesn't recognize the exception.
ErrorClassifier = Callable[[Exception], "str | None"]


@dataclass(frozen=True)
class Connector:
    """Describes one connector: its tools, mount, and upstream identity."""

    slug: str
    tool_prefix: str
    display_name: str
    upstream_provider: str
    register: Callable[[ToolRegistry, Settings], None]
    upstream: UpstreamAuthStrategy | None = None
    error_classifier: ErrorClassifier | None = None

    def matches(self, spec: ToolSpec) -> bool:
        """Whether a tool spec belongs to this connector."""
        return spec.name.startswith(self.tool_prefix)

    def connect_url(self, settings: Settings, external_user_id: str) -> str | None:
        """Return a user-facing URL to (re)connect this connector, if it has one."""
        if self.upstream is None:
            return None
        return self.upstream.connect_url(settings, external_user_id)

    def classify_error(self, exc: Exception) -> str | None:
        """Return a stable error code for a connector-specific exception, if known."""
        if self.error_classifier is None:
            return None
        return self.error_classifier(exc)
