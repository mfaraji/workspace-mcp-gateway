"""Per-request identity propagation into MCP tool handlers.

The MCP Streamable HTTP transport invokes tool handlers without passing the HTTP
request through. We bridge that gap with an ASGI middleware that:

1. resolves the authenticated user from request headers at the transport edge,
2. rejects the request with HTTP 401 if no trustworthy identity is present, and
3. stashes the resolved identity in a ``ContextVar`` that tool handlers read via
   :func:`require_current_user`.

Keeping this in one module means a change in how identity is carried (e.g. a
future verified OAuth identity) only touches here.
"""

from __future__ import annotations

import json
from contextvars import ContextVar

from starlette.types import ASGIApp, Receive, Scope, Send

from gateway.config import Settings
from gateway.identity.models import AuthenticatedUser, IdentityError
from gateway.identity.resolver import resolve_identity

_current_user: ContextVar[AuthenticatedUser | None] = ContextVar(
    "current_authenticated_user", default=None
)


def require_current_user() -> AuthenticatedUser:
    """Return the identity resolved for the current request, or raise.

    Raises:
        IdentityError: if called outside an authenticated request scope.
    """
    user = _current_user.get()
    if user is None:
        raise IdentityError("no authenticated user in the current request context")
    return user


class IdentityMiddleware:
    """ASGI middleware enforcing identity on every wrapped request."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1"): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        try:
            auth = resolve_identity(headers, self.settings)
        except IdentityError as exc:
            await _send_401(send, str(exc))
            return

        token = _current_user.set(auth)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_user.reset(token)


async def _send_401(send: Send, detail: str) -> None:
    body = json.dumps({"error": "unauthorized", "detail": detail}).encode()
    # Advertise Bearer so native MCP clients know to authenticate with a token.
    www_authenticate = f'Bearer error="invalid_token", error_description="{detail}"'
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", www_authenticate.encode("latin-1", "replace")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
