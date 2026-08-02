"""HTTP client helper for the Apex invoicing/timesheet REST API.

Apex has no OAuth of its own, so each user authenticates with their own API
key (``Authorization: Bearer pk_live_...``), stored the same way an OAuth
access token is for the Google providers — see
:class:`~gateway.connectors.upstream.ApiKeyStrategy` and ``APEX_API_KEY``
below. See ``docs/api/invoice-rest.openapi.yaml`` in the apex repo for the
full route/schema reference this client implements.
"""

from __future__ import annotations

from typing import Any

import httpx

from gateway.config import Settings
from gateway.connectors.upstream import ApiKeyStrategy

PROVIDER = "apex"

_STATUS_ERROR_CODES = {
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    429: "rate_limited",
}

# Falls back to the deploy-wide APEX_API_KEY setting until a user stores
# their own personal key with the apex_set_api_key tool.
APEX_API_KEY = ApiKeyStrategy(
    upstream_provider=PROVIDER,
    display_name="Apex",
    set_key_tool="apex_set_api_key",
    fallback_key=lambda settings: settings.apex_api_key,
)


def apex_request(
    settings: Settings,
    api_key: str,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
) -> Any:
    """Call the Apex REST API with a resolved API key and return the decoded JSON body.

    Raises ``gateway.providers.registry.ToolError`` on transport failures or
    non-2xx responses, classified from the HTTP status the same way the
    registry classifies Google API errors.
    """
    from gateway.providers.registry import ToolError

    url = f"{settings.apex_base_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {api_key}"}
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
