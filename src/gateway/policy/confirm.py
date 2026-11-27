"""Policy + confirmation layer.

Read tools execute immediately. Mutating tools have a risk tier: ``LOW`` executes
directly, ``MEDIUM`` requires a two-step confirmation. The first call returns a
preview plus a signed ``confirmation_token``; the model must ask the *user* the
question in chat and, only after an explicit yes, call again with that token. The
tier defaults per tool (``ToolSpec.default_tier``) and each user may override it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from gateway.config import Settings
from gateway.providers.base import CallContext, RiskLevel, RiskTier, ToolSpec

_CONFIRM_SALT = "tool-confirmation"


@dataclass
class Allow:
    """The tool may execute now."""


@dataclass
class NeedsConfirmation:
    """The tool must be re-invoked with ``confirmation_token`` to execute."""

    preview: str
    confirmation_token: str


PolicyDecision = Allow | NeedsConfirmation


def _args_fingerprint(args: dict) -> str:
    raw = json.dumps(args, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt=_CONFIRM_SALT)


def mint_confirmation_token(
    settings: Settings, ctx: CallContext, tool_name: str, args: dict
) -> str:
    """Mint a short-lived token binding (user, tool, args) for confirmation."""
    payload = {
        "uid": ctx.external_user_id,
        "tool": tool_name,
        "fp": _args_fingerprint(args),
    }
    return _serializer(settings).dumps(payload)


def verify_confirmation_token(
    settings: Settings, token: str, ctx: CallContext, tool_name: str, args: dict
) -> bool:
    """Return True if ``token`` is a valid confirmation for this exact call."""
    try:
        payload = _serializer(settings).loads(
            token, max_age=settings.confirmation_token_ttl_seconds
        )
    except (SignatureExpired, BadSignature):
        return False
    return (
        payload.get("uid") == ctx.external_user_id
        and payload.get("tool") == tool_name
        and payload.get("fp") == _args_fingerprint(args)
    )


def check(
    settings: Settings,
    spec: ToolSpec,
    args: dict,
    ctx: CallContext,
    *,
    confirmation_token: str | None = None,
    preview_builder=None,
    tier: RiskTier | None = None,
    display_name: str | None = None,
) -> PolicyDecision:
    """Decide whether a tool call may proceed.

    Read tools and LOW-tier mutating tools are always allowed. MEDIUM-tier tools
    require a valid confirmation token; absent one, a ``NeedsConfirmation`` carrying
    a fresh token (and an optional human-readable preview) is returned. ``tier``
    is the user's effective tier; ``None`` means the tool's default. ``display_name`` is
    the name this mount shows the model, used only in the fallback preview.
    """
    tier = tier or spec.default_tier
    if spec.risk is RiskLevel.READ or tier is RiskTier.LOW:
        return Allow()

    if confirmation_token and verify_confirmation_token(
        settings, confirmation_token, ctx, spec.name, args
    ):
        return Allow()

    preview = preview_builder(args) if preview_builder else f"Confirm {display_name or spec.name}"
    token = mint_confirmation_token(settings, ctx, spec.name, args)
    return NeedsConfirmation(preview=preview, confirmation_token=token)
