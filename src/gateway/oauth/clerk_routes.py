"""OAuth protected-resource metadata for every connector mount."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from gateway.config import get_settings
from gateway.connectors.registry import by_slug
from gateway.identity.models import IdentityError
from gateway.oauth.clerk import (
    issuer_url,
)

router = APIRouter()


def _resource(product: str | None = None) -> str:
    settings = get_settings()
    return f"{settings.base_url.rstrip('/')}/mcp" + (f"/{product}" if product else "")


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
@router.get("/.well-known/oauth-protected-resource/mcp/{product}")
async def protected_resource_metadata(product: str | None = None) -> JSONResponse:
    """RFC 9728 discovery for the all-tools and product-specific MCP resources."""
    settings = get_settings()
    if product is not None and by_slug(product) is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    try:
        issuer = issuer_url(settings)
    except IdentityError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(
        {
            "resource": _resource(product),
            "authorization_servers": [issuer],
            "scopes_supported": ["mcp:tools"],
            "bearer_methods_supported": ["header"],
        },
        headers={"Cache-Control": "public, max-age=300"},
    )
