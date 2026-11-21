"""HTTP client helper for the Apex invoicing/timesheet/wealth REST API.

The user signs in to the gateway with Clerk OAuth; that token is audience-bound to
the gateway and is never forwarded. For each Apex call the gateway mints a
short-lived, Apex-only token (:mod:`gateway.oauth.downstream`): ``sub`` is the
verified Clerk user, ``scope`` is limited to the tool, ``wid`` pins the workspace.
Apex authorizes (workspace membership + capability) on every call; this module
additionally caches the user's workspaces so the gateway can hide tools Apex would
refuse and fail fast with a clear error. See ``docs/api/invoice-rest.openapi.yaml``
in the apex repo for the route/schema reference.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from gateway.config import Settings
from gateway.oauth.downstream import SigningKeyError, mint_apex_token

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from gateway.providers.base import CallContext, ToolSpec

PROVIDER = "apex"
WEALTH_PROVIDER = "apex_wealth"

_STATUS_ERROR_CODES = {
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    429: "rate_limited",
}

WORKSPACE_HEADER = "X-Workspace-Id"
WORKSPACE_ID_DESC = "Apex workspace id (from apex_list_workspaces)."

# Downstream scopes (must match the @McpAccessible scopes in the apex repo).
SCOPE_WORKSPACES_READ = "apex:workspaces.read"
SCOPE_CLIENTS_READ = "apex:clients.read"
SCOPE_INVOICING_READ = "apex:invoicing.read"
SCOPE_INVOICING_WRITE = "apex:invoicing.write"
SCOPE_WEALTH_READ = "apex:wealth.read"

# Capability sentinel: workspaces that hold wealth data (PERSONAL kind).
CAP_WEALTH_WORKSPACE = "wealthWorkspace"

_WORKSPACE_CACHE_TTL = 60.0
_WORKSPACE_CACHE_MAX = 1024
_workspace_cache: dict[str, tuple[float, list[dict] | None]] = {}
_workspace_cache_lock = threading.Lock()


@dataclass(frozen=True)
class ApexCredentials:
    """What to send to Apex: the minted bearer token plus the workspace header.

    Both fields are secret-ish; they are excluded from ``repr`` so they cannot
    leak through logs or tracebacks.
    """

    token: str = field(repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    jti: str = ""


def is_wealth_workspace(workspace: dict) -> bool:
    capabilities = [str(c).lower() for c in workspace.get("capabilities") or []]
    return workspace.get("kind") == "PERSONAL" or any("wealth" in c for c in capabilities)


def workspace_has(workspace: dict, capability: str | None) -> bool:
    """Whether one Apex workspace (``{id, kind, capabilities}``) supports ``capability``."""
    if capability is None:
        return True
    if capability == CAP_WEALTH_WORKSPACE:
        return is_wealth_workspace(workspace)
    return capability in (workspace.get("capabilities") or [])


def _mint(
    settings: Settings,
    subject: str | None,
    client_id: str | None,
    scopes: list[str],
    workspace_id: str | None,
) -> tuple[str, str]:
    from gateway.providers.registry import ToolError

    if not subject:
        raise ToolError(
            "not_connected", "Apex needs a signed-in Clerk user; sign in to the gateway via OAuth."
        )
    try:
        return mint_apex_token(
            settings,
            subject=subject,
            client_id=client_id,
            workspace_id=workspace_id,
            scopes=scopes,
        )
    except SigningKeyError as exc:
        raise ToolError("provider_error", "Apex access is not configured on the gateway") from exc


def apex_request(
    settings: Settings,
    token: str,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    """Call the Apex REST API with a minted token and return the decoded JSON body.

    Raises ``gateway.providers.registry.ToolError`` on transport failures or
    non-2xx responses, classified from the HTTP status the same way the
    registry classifies Google API errors.
    """
    from gateway.providers.registry import ToolError

    url = f"{settings.apex_base_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {token}"}
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
    """:func:`apex_request` using resolved :class:`ApexCredentials`."""
    return apex_request(
        settings,
        creds.token,
        method,
        path,
        params=params,
        json=json,
        extra_headers=creds.headers or None,
    )


def user_workspaces(
    settings: Settings, subject: str | None, client_id: str | None
) -> list[dict] | None:
    """The caller's Apex workspaces (``{id, name, kind, capabilities}``), cached ~60s.

    Returns ``None`` when the caller is not an Apex user (Apex answered 401) or Apex
    could not be reached; callers treat that as "nothing permitted". Only the
    successful answer and the 401 are cached, so an outage heals on the next call.
    """
    if not subject:
        return None
    now = time.monotonic()
    with _workspace_cache_lock:
        hit = _workspace_cache.get(subject)
        if hit is not None and now - hit[0] < _WORKSPACE_CACHE_TTL:
            return hit[1]

    from gateway.providers.registry import ToolError

    token, _ = _mint(settings, subject, client_id, [SCOPE_WORKSPACES_READ], None)
    result: list[dict] | None
    try:
        data = apex_request(settings, token, "GET", "/api/workspaces")
        result = [
            {
                "id": w.get("id"),
                "name": w.get("name"),
                "kind": w.get("kind"),
                "capabilities": w.get("capabilities") or [],
            }
            for w in (data if isinstance(data, list) else [])
            if isinstance(w, dict) and w.get("id")
        ]
    except ToolError as exc:
        if exc.error_code != "unauthorized":
            return None  # transient: do not cache
        result = None  # not an Apex user

    with _workspace_cache_lock:
        if len(_workspace_cache) >= _WORKSPACE_CACHE_MAX:
            _workspace_cache.clear()
        _workspace_cache[subject] = (now, result)
    return result


def clear_workspace_cache() -> None:
    with _workspace_cache_lock:
        _workspace_cache.clear()


def apex_credentials(
    settings: Settings,
    ctx: CallContext,
    *,
    scopes: list[str],
    workspace_id: str,
    capability: str | None,
) -> ApexCredentials:
    """Validate ``workspace_id`` against the user's workspaces, then mint credentials.

    Unknown workspace -> ``invalid_input``; a workspace without ``capability`` ->
    ``forbidden`` (no Apex request is made). Apex re-checks both on the call itself.
    """
    from gateway.providers.registry import ToolError

    workspaces = user_workspaces(settings, ctx.clerk_subject, ctx.oauth_client_id)
    if workspaces is None:
        raise ToolError("forbidden", "Apex does not recognize your account or is unreachable")
    chosen = next((w for w in workspaces if w["id"] == workspace_id), None)
    if chosen is None:
        raise ToolError(
            "invalid_input",
            "workspace_id is not one of your Apex workspaces; call apex_list_workspaces.",
        )
    if not workspace_has(chosen, capability):
        raise ToolError(
            "forbidden",
            f"Apex workspace '{chosen['name']}' ({chosen['kind']}) does not support this tool",
        )
    token, jti = _mint(settings, ctx.clerk_subject, ctx.oauth_client_id, scopes, workspace_id)
    return ApexCredentials(token, {WORKSPACE_HEADER: workspace_id}, jti)


class ApexUpstream:
    """Connector upstream for Apex: nothing is stored; credentials are minted per call.

    ``permits`` is the per-user permission hook consulted by the access policy when
    listing and calling tools: a tool is permitted only if one of the caller's Apex
    workspaces has the tool's ``required_capability``.
    """

    def __init__(self, *, upstream_provider: str, display_name: str) -> None:
        self.upstream_provider = upstream_provider
        self.display_name = display_name

    def get_credentials(self, session: Session, ctx: CallContext, settings: Settings):
        raise NotImplementedError("Apex credentials are minted per call; use apex_credentials")

    def connect_url(self, settings: Settings, external_user_id: str) -> str | None:
        return None

    def permits(
        self, settings: Settings, subject: str | None, client_id: str | None, spec: ToolSpec
    ) -> bool:
        workspaces = user_workspaces(settings, subject, client_id)
        if workspaces is None:
            return False
        if spec.required_capability is None:
            return True
        return any(workspace_has(w, spec.required_capability) for w in workspaces)


APEX_UPSTREAM = ApexUpstream(upstream_provider=PROVIDER, display_name="Apex")
APEX_WEALTH_UPSTREAM = ApexUpstream(upstream_provider=WEALTH_PROVIDER, display_name="Apex Wealth")
