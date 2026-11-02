"""HTTP client helper for the Apex invoicing/timesheet REST API.

Apex has no OAuth of its own, so each user authenticates with their own API
key (``Authorization: Bearer pk_live_...``), stored the same way an OAuth
access token is for the Google providers, or the gateway acts as the signed-in
Clerk user with a system key plus ``X-Act-As-Subject`` — see
:class:`ApexAuthStrategy` and ``APEX_API_KEY`` below. See
``docs/api/invoice-rest.openapi.yaml`` in the apex repo for the
full route/schema reference this client implements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from gateway.config import Settings
from gateway.connectors.apikey import get_api_key_connection, get_default_workspace
from gateway.connectors.upstream import ApiKeyStrategy
from gateway.crypto.tokens import get_cipher

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from gateway.providers.base import CallContext

PROVIDER = "apex"

_STATUS_ERROR_CODES = {
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    429: "rate_limited",
}

ACT_AS_SUBJECT_HEADER = "X-Act-As-Subject"
WORKSPACE_HEADER = "X-Workspace-Id"

WEALTH_PROVIDER = "apex_wealth"


@dataclass(frozen=True)
class ApexCredentials:
    """What to send to Apex: the bearer key plus any extra (act-as) headers.

    Both fields are secret-ish; they are excluded from ``repr`` so they cannot
    leak through logs or tracebacks.
    """

    api_key: str = field(repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)


class ApexAuthStrategy(ApiKeyStrategy):
    """Apex credentials: stored per-user key, else Clerk act-as, else not_connected.

    Subclasses :class:`ApiKeyStrategy` so the ``POST /connectors/{slug}/apikey``
    intake route keeps working. Act-as keys only on ``ctx.clerk_subject``.
    """

    def get_credentials(  # type: ignore[override]
        self,
        session: Session,
        ctx: CallContext,
        settings: Settings,
        *,
        with_workspace: bool = True,
    ) -> ApexCredentials:
        from gateway.providers.registry import ToolError

        conn = get_api_key_connection(session, ctx.user_id, self.upstream_provider)
        if conn is not None and conn.token is not None:
            return ApexCredentials(get_cipher().decrypt(conn.token.encrypted_access_token))

        gateway_key = settings.apex_gateway_api_key
        if ctx.clerk_subject and gateway_key:
            headers = {ACT_AS_SUBJECT_HEADER: ctx.clerk_subject}
            if with_workspace:
                workspace = get_default_workspace(session, ctx.user_id, self.upstream_provider)
                if workspace is None:
                    raise ToolError(
                        "workspace_not_set",
                        f"no default {self.display_name} workspace chosen; call "
                        f"{self.list_workspaces_tool} to see your workspaces, then "
                        f"{self.set_workspace_tool} with the workspace_id to use.",
                    )
                headers[WORKSPACE_HEADER] = workspace["workspace_id"]
            return ApexCredentials(gateway_key, headers)

        raise ToolError(
            "not_connected",
            f"{self.display_name} is not connected: either sign in with a linked Clerk "
            f"account (no key needed), or store a personal API key with the "
            f"{self.set_key_tool} tool.",
        )

    def __init__(self, *, set_workspace_tool: str, list_workspaces_tool: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.set_workspace_tool = set_workspace_tool
        self.list_workspaces_tool = list_workspaces_tool


# Per-user keys, else Clerk act-as. No deploy-wide fallback (apex_api_key is
# deprecated and ignored).
APEX_API_KEY = ApexAuthStrategy(
    upstream_provider=PROVIDER,
    display_name="Apex",
    set_key_tool="apex_set_api_key",
    set_workspace_tool="apex_set_workspace",
    list_workspaces_tool="apex_list_workspaces",
)

# Wealth data only exists in PERSONAL workspaces, whereas the invoicing key is
# typically for a BUSINESS workspace, so wealth has its own key and default workspace.
APEX_WEALTH_API_KEY = ApexAuthStrategy(
    upstream_provider=WEALTH_PROVIDER,
    display_name="Apex Wealth",
    set_key_tool="apex_wealth_set_api_key",
    set_workspace_tool="apex_wealth_set_workspace",
    list_workspaces_tool="apex_wealth_list_workspaces",
)


def apex_request(
    settings: Settings,
    api_key: str,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    """Call the Apex REST API with a resolved API key and return the decoded JSON body.

    Raises ``gateway.providers.registry.ToolError`` on transport failures or
    non-2xx responses, classified from the HTTP status the same way the
    registry classifies Google API errors.
    """
    from gateway.providers.registry import ToolError

    url = f"{settings.apex_base_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {api_key}"}
    if extra_headers:
        headers.update(extra_headers)
    try:
        response = httpx.request(
            method, url, params=params, json=json, headers=headers, timeout=30.0
        )
    except httpx.HTTPError as exc:
        raise ToolError("provider_error", f"apex request failed: {exc}") from exc

    if response.status_code >= 400:
        raise ToolError(
            _STATUS_ERROR_CODES.get(response.status_code, "provider_error"),
            f"apex request failed ({response.status_code}): {response.text[:500]}",
        )
    if not response.content:
        return None
    return response.json()


def apex_call(
    settings: Settings,
    creds: ApexCredentials,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
) -> Any:
    """:func:`apex_request` using resolved :class:`ApexCredentials` (key + act-as headers)."""
    return apex_request(
        settings,
        creds.api_key,
        method,
        path,
        params=params,
        json=json,
        extra_headers=creds.headers or None,
    )
