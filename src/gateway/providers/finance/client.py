"""HTTP client for the public rates API behind the finance connector.

The API needs no auth and takes numeric currency IDs, so this module also owns
code -> ID resolution (from a 24h-cached currency list) and small in-memory TTL
caches for the sources (60s) and history (5 min) responses.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

BASE_URL = "https://api.trustply.com/api"
TIMEOUT_SECONDS = 10.0

CURRENCIES_TTL = 24 * 3600
SOURCES_TTL = 60
HISTORY_TTL = 300

_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _get(path: str, params: dict[str, Any] | None, ttl: int) -> Any:
    """GET a JSON endpoint with a TTL cache; raises ``ToolError`` on failure."""
    from gateway.providers.registry import ToolError

    key = f"{path}?{sorted((params or {}).items())}"
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    try:
        response = httpx.get(
            f"{BASE_URL}{path}",
            params=params,
            headers={"Accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise ToolError("provider_error", f"rates request failed: {exc}") from exc
    if response.status_code != 200:
        code = "rate_limited" if response.status_code == 429 else "provider_error"
        raise ToolError(
            code, f"rates request failed ({response.status_code}): {response.text[:300]}"
        )
    data = response.json()
    with _lock:
        _cache[key] = (now, data)
    return data


def list_currencies() -> list[dict[str, Any]]:
    data = _get("/money/currency", None, CURRENCIES_TTL)
    return [
        {"id": c["id"], "abbreviation": c["abbreviation"], "title": c.get("title")}
        for c in data.get("currencies", [])
    ]


def resolve_currency(code: str) -> int:
    """Map a currency code (case-insensitive) to the API's numeric ID."""
    from gateway.providers.registry import ToolError

    currencies = list_currencies()
    wanted = code.strip().upper()
    for c in currencies:
        if str(c["abbreviation"]).upper() == wanted:
            return int(c["id"])
    valid = ", ".join(sorted(str(c["abbreviation"]) for c in currencies))
    raise ToolError("invalid_input", f"unknown currency code '{code}'. Valid codes: {valid}")


def get_sources(from_id: int, to_id: int) -> dict[str, Any]:
    return _get("/currency/sources", {"from": from_id, "to": to_id}, SOURCES_TTL)


def get_rates(from_id: int, to_id: int, view: str) -> dict[str, Any]:
    return _get("/currency/rates", {"from": from_id, "to": to_id, "view": view}, HISTORY_TTL)
