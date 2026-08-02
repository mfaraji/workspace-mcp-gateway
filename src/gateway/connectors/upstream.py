"""Upstream (gateway -> service) credential strategies for connectors.

A strategy resolves one connector's upstream credentials for a user, giving
provider tool code a single call path regardless of whether the upstream
authenticates via per-user OAuth (:class:`OAuthStrategy`, generalizing the
existing Google connection machinery) or a per-user API key
(:class:`ApiKeyStrategy`, used by Apex). Each strategy instance is configured
for exactly one connector at construction time.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from gateway.crypto.tokens import get_cipher
from gateway.providers.google.connections import get_active_connection, load_credentials

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from gateway.config import Settings
    from gateway.providers.base import CallContext


class UpstreamAuthStrategy(Protocol):
    """Resolves a user's upstream credentials for one connector."""

    def get_credentials(self, session: Session, ctx: CallContext, settings: Settings):
        """Return usable credentials for this user.

        Raises:
            ToolError: with ``error_code`` ``"not_connected"`` or
                ``"reauth_required"`` when the caller must (re)connect.
        """
        ...

    def connect_url(self, settings: Settings, external_user_id: str) -> str | None:
        """Return a user-facing URL to (re)connect, or ``None`` if there isn't one."""
        ...


class OAuthStrategy:
    """Per-user OAuth, generalizing the existing Google connection machinery.

    ``upstream_provider`` selects which stored account to use. Several
    connectors may share one provider — the Google Calendar, Drive, and Tasks
    connectors all use ``upstream_provider="google"`` so one Google account
    and refresh token covers every one of them, with ``required_scopes``
    gating access per connector (incremental authorization unions the scopes
    of whichever connectors a user has enabled).
    """

    def __init__(
        self,
        *,
        upstream_provider: str,
        required_scopes: list[str],
        display_name: str,
        product: str,
        missing_scope_hint: str = "missing required scopes",
    ) -> None:
        self.upstream_provider = upstream_provider
        self.required_scopes = required_scopes
        self.display_name = display_name
        self.product = product
        self.missing_scope_hint = missing_scope_hint

    def get_credentials(self, session: Session, ctx: CallContext, settings: Settings):
        from gateway.providers.registry import ToolError

        conn = get_active_connection(session, ctx.user_id, self.upstream_provider)
        if conn is None:
            raise ToolError(
                "not_connected",
                f"no active {self.display_name} connection; authorize here: "
                f"{self.connect_url(settings, ctx.external_user_id)}",
            )

        missing_scopes = sorted(set(self.required_scopes) - set(conn.scopes or []))
        if missing_scopes:
            raise ToolError(
                "reauth_required",
                f"{self.display_name} connection is {self.missing_scope_hint}; "
                f"reconnect here: {self.connect_url(settings, ctx.external_user_id)}",
            )

        creds = load_credentials(session, conn, settings)
        conn.last_used_at = datetime.now(UTC)
        return creds

    def connect_url(self, settings: Settings, external_user_id: str) -> str:
        from gateway.oauth.google import build_start_url

        return build_start_url(settings, external_user_id, product=self.product)


class ApiKeyStrategy:
    """Per-user API key for an upstream with no OAuth of its own.

    The user pastes a key (via the connector's ``set_key_tool`` MCP tool, or
    the ``POST /connectors/{slug}/apikey`` HTTP route) and it's encrypted at
    rest exactly like an OAuth access token. ``fallback_key`` is an optional
    deploy-wide key used until a given user has stored their own — e.g.
    Apex's ``APEX_API_KEY`` setting, kept as a transitional default.
    """

    def __init__(
        self,
        *,
        upstream_provider: str,
        display_name: str,
        set_key_tool: str,
        fallback_key: Callable[[Settings], str | None] | None = None,
    ) -> None:
        self.upstream_provider = upstream_provider
        self.display_name = display_name
        self.set_key_tool = set_key_tool
        self.fallback_key = fallback_key

    def get_credentials(self, session: Session, ctx: CallContext, settings: Settings) -> str:
        from gateway.providers.registry import ToolError

        conn = get_active_connection(session, ctx.user_id, self.upstream_provider)
        if conn is not None and conn.token is not None:
            return get_cipher().decrypt(conn.token.encrypted_access_token)

        fallback = self.fallback_key(settings) if self.fallback_key else None
        if fallback:
            return fallback

        raise ToolError(
            "not_connected",
            f"no {self.display_name} API key on file; set one with the "
            f"{self.set_key_tool} tool.",
        )

    def connect_url(self, settings: Settings, external_user_id: str) -> str | None:
        # No redirect flow — guidance is in the ToolError message.
        return None
