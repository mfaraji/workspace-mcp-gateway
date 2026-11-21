"""Gateway-issued, resource-bound tokens for downstream APIs (Apex).

The user's Clerk access token is audience-bound to this gateway and must never be
forwarded (MCP token passthrough is forbidden). Instead, once the Clerk token has
been verified, the gateway mints a short-lived JWT that is valid only for Apex's MCP
REST API: ``sub`` is the verified Clerk user, ``act`` names this gateway (RFC 8693
delegation style), ``scope`` is limited to what the tool needs and ``wid`` pins the
workspace. Apex verifies it against the public key published at
``/.well-known/apex-jwks.json``.

Minted tokens are secrets: never log or audit them (audit ``jti``/scopes/``wid`` only).
"""

from __future__ import annotations

import base64
import hashlib
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from gateway.config import Settings

JWKS_PATH = "/.well-known/apex-jwks.json"


class SigningKeyError(RuntimeError):
    """The gateway signing key is missing or unusable."""


@dataclass(frozen=True)
class _Signer:
    private_key: object
    algorithm: str
    kid: str
    jwk: dict


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_int(value: int, length: int | None = None) -> str:
    size = length or max(1, (value.bit_length() + 7) // 8)
    return _b64url(value.to_bytes(size, "big"))


def _build_signer(pem: str) -> _Signer:
    # Env files often store PEMs on one line with literal "\n".
    normalized = pem.replace("\\n", "\n").strip().encode("utf-8")
    try:
        key = serialization.load_pem_private_key(normalized, password=None)
    except (ValueError, TypeError) as exc:
        raise SigningKeyError("GATEWAY_SIGNING_KEY is not a valid unencrypted PEM key") from exc

    public = key.public_key()
    if isinstance(key, ec.EllipticCurvePrivateKey):
        if not isinstance(key.curve, ec.SECP256R1):
            raise SigningKeyError("GATEWAY_SIGNING_KEY EC key must use curve P-256")
        numbers = public.public_numbers()
        jwk = {
            "kty": "EC",
            "crv": "P-256",
            "x": _b64url_int(numbers.x, 32),
            "y": _b64url_int(numbers.y, 32),
        }
        algorithm = "ES256"
    elif isinstance(key, rsa.RSAPrivateKey):
        if key.key_size < 2048:
            raise SigningKeyError("GATEWAY_SIGNING_KEY RSA key must be at least 2048 bits")
        numbers = public.public_numbers()
        jwk = {"kty": "RSA", "n": _b64url_int(numbers.n), "e": _b64url_int(numbers.e)}
        algorithm = "RS256"
    else:
        raise SigningKeyError("GATEWAY_SIGNING_KEY must be an EC P-256 or RSA key")

    der = public.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    kid = _b64url(hashlib.sha256(der).digest())[:16]
    jwk.update({"use": "sig", "alg": algorithm, "kid": kid})
    return _Signer(private_key=key, algorithm=algorithm, kid=kid, jwk=jwk)


@lru_cache(maxsize=4)
def _signer_for(pem: str) -> _Signer:
    return _build_signer(pem)


def _signer(settings: Settings) -> _Signer:
    if settings.gateway_signing_key is None:
        raise SigningKeyError("GATEWAY_SIGNING_KEY is not configured")
    pem = settings.gateway_signing_key.get_secret_value()
    if not pem.strip():
        raise SigningKeyError("GATEWAY_SIGNING_KEY is not configured")
    return _signer_for(pem)


def jwks(settings: Settings) -> dict:
    """The public JWK set (never contains private material)."""
    return {"keys": [_signer(settings).jwk]}


def mint_apex_token(
    settings: Settings,
    *,
    subject: str,
    client_id: str | None,
    workspace_id: str | None,
    scopes: list[str],
) -> tuple[str, str]:
    """Mint a short-lived Apex-only token. Returns ``(token, jti)``.

    ``subject`` is the verified Clerk ``sub``. ``client_id`` is the OAuth client the
    user signed in with (recorded in ``act`` for traceability). ``workspace_id`` is
    pinned in ``wid`` when the call targets one workspace.
    """
    if not subject:
        raise SigningKeyError("cannot mint a downstream token without a verified subject")
    if not scopes:
        raise SigningKeyError("cannot mint a downstream token without scopes")
    signer = _signer(settings)
    issuer = settings.base_url.rstrip("/")
    now = int(time.time())
    jti = uuid.uuid4().hex
    act: dict[str, str] = {"sub": issuer}
    if client_id:
        act["client_id"] = client_id
    claims: dict = {
        "iss": issuer,
        "aud": settings.apex_mcp_audience,
        "sub": subject,
        "act": act,
        "scope": " ".join(scopes),
        "jti": jti,
        "iat": now,
        "nbf": now,
        "exp": now + settings.apex_token_ttl_seconds,
    }
    if workspace_id:
        claims["wid"] = workspace_id
    token = jwt.encode(
        claims,
        signer.private_key,  # type: ignore[arg-type]
        algorithm=signer.algorithm,
        headers={"kid": signer.kid, "typ": "at+jwt"},
    )
    return token, jti
