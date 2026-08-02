"""HTTP intake route for per-user API keys.

Complements the ``<slug>_set_api_key`` MCP tool with a plain HTTP endpoint —
useful for a settings UI that doesn't go through an MCP client. Resolves the
caller the same way the MCP mounts and the Google OAuth routes do: trusted
Open WebUI headers or a native-client bearer token.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from gateway.config import get_settings
from gateway.connectors.apikey import store_api_key
from gateway.connectors.registry import by_slug
from gateway.connectors.upstream import ApiKeyStrategy
from gateway.db.engine import session_scope
from gateway.identity.models import IdentityError
from gateway.identity.resolver import get_or_create_user, resolve_identity

router = APIRouter(prefix="/connectors", tags=["connectors"])


class SetApiKeyBody(BaseModel):
    api_key: str = Field(..., min_length=1)


@router.post("/{slug}/apikey")
async def set_api_key(slug: str, body: SetApiKeyBody, request: Request) -> JSONResponse:
    """Store the calling user's API key for one API-key-based connector."""
    settings = get_settings()
    try:
        auth = resolve_identity(dict(request.headers), settings)
    except IdentityError as exc:
        return JSONResponse({"error": "unauthorized", "detail": str(exc)}, status_code=401)

    connector = by_slug(slug)
    if connector is None or not isinstance(connector.upstream, ApiKeyStrategy):
        return JSONResponse(
            {"error": "not_found", "detail": f"no API-key connector '{slug}'"}, status_code=404
        )

    with session_scope() as session:
        user = get_or_create_user(session, auth)
        store_api_key(session, user.id, connector.upstream_provider, body.api_key)

    return JSONResponse({"connected": True, "connector": slug})
