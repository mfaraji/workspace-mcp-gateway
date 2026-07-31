"""HTTP client helper for the Apex invoicing/timesheet REST API.

Apex authenticates with a single workspace-scoped API key (``Authorization:
Bearer pk_live_...``), not per-user OAuth, so unlike the Google providers there
is no ``ProviderConnection`` to resolve — every call uses the same configured
key. See ``docs/api/invoice-rest.openapi.yaml`` in the apex repo for the full
route/schema reference this client implements.
"""

from __future__ import annotations

from typing import Any

import httpx

from gateway.config import Settings

PROVIDER = "apex"

_STATUS_ERROR_CODES = {
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    429: "rate_limited",
}


def apex_request(
    settings: Settings,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
) -> Any:
    """Call the Apex REST API and return the decoded JSON body.

    Raises ``gateway.providers.registry.ToolError`` on transport failures or
    non-2xx responses, classified from the HTTP status the same way the
    registry classifies Google API errors.
    """
    from gateway.providers.registry import ToolError

    url = f"{settings.apex_base_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {settings.apex_api_key}"}
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
