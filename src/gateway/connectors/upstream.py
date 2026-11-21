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
from gateway.providers.google.connections import (
    get_active_connection,
    list_connections,
    load_credentials,
    resolve_connection,
)

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

    ``upstream_provider`` + ``product`` select the stored accounts. Google connectors
    share ``upstream_provider="google"`` but each product (calendar, drive, tasks,
    gmail) keeps its own set of connected accounts: a user may connect several
    accounts to one product and none to another. A call uses the account named in
    the tool input, else the product's default account.
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

    def get_connection_credentials(
        self, session: Session, ctx: CallContext, settings: Settings
    ):
        """Resolve ``(credentials, connection)`` for this product and ``ctx.account``.

        The account is the one named by the tool input's ``account`` (email), else the
        product's default account.
        """
        from gateway.providers.registry import ToolError

        conn = resolve_connection(
            session, ctx.user_id, self.upstream_provider, self.product, ctx.account
        )
        if conn is None:
            if ctx.account:
                known = sorted(
                    c.provider_email or c.provider_account_id
                    for c in list_connections(
                        session, ctx.user_id, self.upstream_provider, self.product
                    )
                    if c.status == "active"
                )
                hint = ", ".join(known) if known else "none"
                raise ToolError(
                    "account_not_connected",
                    f"{self.display_name} has no connected account '{ctx.account}'; "
                    f"connected accounts: {hint}. Add one here: "
                    f"{self.connect_url(settings, ctx.external_user_id)}",
                )
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
        return creds, conn

    def get_credentials(self, session: Session, ctx: CallContext, settings: Settings):
        return self.get_connection_credentials(session, ctx, settings)[0]

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
