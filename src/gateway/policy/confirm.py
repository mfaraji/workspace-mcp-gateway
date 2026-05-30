"""Policy + confirmation layer.

Read tools are allowed to execute immediately. Mutating tools (enabled in a later
milestone) require a two-step confirmation: the first call returns a preview plus
a short-lived signed ``confirmation_token``; the model must call again with that
token to execute. This module owns minting/verifying those tokens so that turning
a tool from read to mutating is a localized change.

For the current vertical slice only read tools exist, so :func:`check` returns
``Allow`` for them. The confirm-token machinery is implemented and unit-testable
but not yet exercised by a registered tool.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from gateway.config import Settings
from gateway.providers.base import CallContext, RiskLevel, ToolSpec

_CONFIRM_SALT = "tool-confirmation"
_CONFIRM_MAX_AGE_SECONDS = 60


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
        payload = _serializer(settings).loads(token, max_age=_CONFIRM_MAX_AGE_SECONDS)
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
) -> PolicyDecision:
    """Decide whether a tool call may proceed.

    Read tools are always allowed. Mutating tools require a valid confirmation
    token; absent one, a ``NeedsConfirmation`` carrying a fresh token (and an
    optional human-readable preview) is returned.
    """
    if spec.risk is RiskLevel.READ:
        return Allow()

    if confirmation_token and verify_confirmation_token(
        settings, confirmation_token, ctx, spec.name, args
    ):
        return Allow()

    preview = preview_builder(args) if preview_builder else f"Confirm {spec.name}"
    token = mint_confirmation_token(settings, ctx, spec.name, args)
    return NeedsConfirmation(preview=preview, confirmation_token=token)
