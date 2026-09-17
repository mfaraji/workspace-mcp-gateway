"""HTTP client for the Telegram Bot API.

Each user authenticates with their own bot token from @BotFather, stored the
same way an OAuth access token is for Google — see
:class:`~gateway.connectors.upstream.ApiKeyStrategy` and ``TELEGRAM_API_KEY``.
"""

from __future__ import annotations

from typing import Any

import httpx

from gateway.config import Settings
from gateway.connectors.upstream import ApiKeyStrategy

PROVIDER = "telegram"
TELEGRAM_API_BASE = "https://api.telegram.org"

_STATUS_ERROR_CODES = {
    400: "invalid_input",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    429: "rate_limited",
}

TELEGRAM_API_KEY = ApiKeyStrategy(
    upstream_provider=PROVIDER,
    display_name="Telegram",
    set_key_tool="telegram_set_api_key",
    fallback_key=lambda settings: settings.telegram_bot_token,
)


def telegram_request(
    settings: Settings,
    bot_token: str,
    method: str,
    *,
    json: dict[str, Any] | None = None,
) -> Any:
    """Call the Telegram Bot API and return the decoded JSON body.

    Raises ``gateway.providers.registry.ToolError`` on transport failures or
    non-2xx / ``ok: false`` responses.
    """
    from gateway.providers.registry import ToolError

    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/{method}"
    try:
        response = httpx.post(url, json=json, timeout=30.0)
    except httpx.HTTPError as exc:
        raise ToolError("provider_error", f"telegram request failed: {exc}") from exc

    if response.status_code >= 400:
        detail = response.text[:500] if response.text else str(response.status_code)
        raise ToolError(
            _STATUS_ERROR_CODES.get(response.status_code, "provider_error"),
            f"telegram request failed ({response.status_code}): {detail}",
        )

    if not response.content:
        return None

    body = response.json()
    if isinstance(body, dict) and body.get("ok") is False:
        description = str(body.get("description", "unknown error"))[:500]
        raise ToolError("provider_error", f"telegram API error: {description}")

    return body
