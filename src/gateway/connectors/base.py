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

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
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
class Bundle:
    """An extra ``/mcp/<slug>`` endpoint that serves several connectors' tools at once.

    A bundle owns no tools and no credentials: it advertises the union of its member
    connectors' tools, and every call is still granted, audited and authorized per tool
    and per member connector. Members are all connectors stored under ``upstream_provider``
    (so a new Google connector joins the Google bundle automatically).
    """

    slug: str
    display_name: str
    upstream_provider: str


class AccessMode(StrEnum):
    """Explicit access policy, independent of upstream credentials."""

    PERSONAL = "personal"
    AUTHENTICATED_SHARED = "authenticated_shared"


@dataclass(frozen=True)
class Connector:
    """Describes one connector: its tools, mount, and upstream identity."""

    slug: str
    tool_prefix: str
    display_name: str
    upstream_provider: str
    register: Callable[[ToolRegistry, Settings], None]
    access_mode: AccessMode = AccessMode.PERSONAL
    upstream: UpstreamAuthStrategy | None = None
    error_classifier: ErrorClassifier | None = None
    # Tool-name prefixes owned by a sibling connector whose own prefix extends this one
    # (e.g. ``apex_`` vs ``apex_wealth_``).
    exclude_prefixes: tuple[str, ...] = ()
    # Internal tool name -> the bare name Google's own MCP server uses. When set, this
    # connector's own ``/mcp/<slug>`` mount exposes exactly these tools under those names
    # (no widgets, no extras); grants, tiers and audit keep the internal names.
    google_names: Mapping[str, str] = field(default_factory=dict)
    # A hidden connector is a mount kept only so existing clients keep working. It owns no
    # tools (``by_spec`` skips it), is not listed on the access or connections pages, and
    # serves the tools of the connector named by ``alias_of``.
    hidden: bool = False
    alias_of: str | None = None

    def matches(self, spec: ToolSpec) -> bool:
        """Whether a tool spec belongs to this connector."""
        return spec.name.startswith(self.tool_prefix) and not spec.name.startswith(
            self.exclude_prefixes
        )

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
