"""Clerk OAuth discovery and resource-bound JWT verification.

The issuer is deployment configuration, never request-controlled. The JWKS URL
must stay on that issuer's origin, avoiding discovery-based SSRF or key swaps.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from urllib.parse import urlparse

import httpx
import jwt

from gateway.config import Settings
from gateway.identity.models import IdentityError

logger = logging.getLogger(__name__)


def issuer_url(settings: Settings) -> str:
    """Return the configured HTTPS issuer, or raise if OAuth is disabled."""
    issuer = (settings.clerk_issuer or "").rstrip("/")
    parsed = urlparse(issuer)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise IdentityError("OAuth issuer is not configured as HTTPS")
    return issuer


@lru_cache(maxsize=8)
def discovery(issuer: str) -> dict:
    """Fetch and cache the issuer's authorization-server metadata."""
    url = f"{issuer}/.well-known/oauth-authorization-server"
    try:
        response = httpx.get(url, timeout=5, follow_redirects=False)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise IdentityError("OAuth authorization server unavailable") from exc
    if not isinstance(data, dict) or data.get("issuer", "").rstrip("/") != issuer:
        raise IdentityError("OAuth issuer metadata mismatch")
    return data


@lru_cache(maxsize=8)
def _jwk_client(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, cache_jwk_set=True, lifespan=300)


def _issuer_endpoint(settings: Settings, field: str) -> str:
    issuer = issuer_url(settings)
    url = discovery(issuer).get(field)
    parsed = urlparse(url or "")
    origin = urlparse(issuer)
    if parsed.scheme != "https" or parsed.netloc != origin.netloc:
        raise IdentityError(f"OAuth {field} endpoint is not on issuer origin")
    return url


def authorization_endpoint(settings: Settings) -> str:
    return _issuer_endpoint(settings, "authorization_endpoint")


def token_endpoint(settings: Settings) -> str:
    return _issuer_endpoint(settings, "token_endpoint")


def verify_access_token(
    token: str, settings: Settings, resource: str, *, required_scope: str | None = "mcp:tools"
) -> str:
    """Verify Clerk signature, issuer, expiry, resource audience, scope and subject."""
    if len(token) > 16384:
        raise IdentityError("OAuth access token too large")
    issuer = issuer_url(settings)
    jwks_url = _issuer_endpoint(settings, "jwks_uri")
    try:
        key = _jwk_client(jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256", "ES256"],
            issuer=issuer,
            audience=resource,
            options={"require": ["iss", "sub", "aud", "exp"]},
            leeway=30,
        )
    except (jwt.PyJWTError, ValueError, httpx.HTTPError) as exc:
        # Keep token material out of logs while retaining the failure category
        # needed to diagnose audience and signature mismatches.
        if isinstance(exc, jwt.MissingRequiredClaimError):
            claim = exc.claim if exc.claim in {"iss", "sub", "aud", "exp"} else "unknown"
            logger.info("OAuth access token rejected: missing required claim %s", claim)
        else:
            logger.info("OAuth access token rejected: %s", type(exc).__name__)
        raise IdentityError("invalid OAuth access token") from exc
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        raise IdentityError("invalid OAuth subject")
    scopes = claims.get("scope", "")
    if isinstance(scopes, str):
        scope_set = set(scopes.split())
    elif isinstance(scopes, list) and all(isinstance(scope, str) for scope in scopes):
        scope_set = set(scopes)
    else:
        raise IdentityError("invalid OAuth scopes")
    if required_scope and required_scope not in scope_set:
        logger.info("OAuth access token rejected: insufficient scope")
        raise IdentityError("insufficient OAuth scope")
    return subject
