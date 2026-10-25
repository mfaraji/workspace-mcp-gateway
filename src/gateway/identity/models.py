"""Identity value objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

IdentitySource = Literal["header", "token", "oauth"]


@dataclass(frozen=True)
class AuthenticatedUser:
    """A verified user identity for the current request.

    ``source`` records how the caller authenticated: ``"header"`` for the trusted
    Open WebUI header path, ``"token"`` for a static client bearer token,
    or ``"oauth"`` for a linked Clerk identity.
    """

    external_user_id: str
    email: str | None = None
    display_name: str | None = None
    source: IdentitySource = "header"
    chat_id: str | None = None
    message_id: str | None = None
    automation_id: str | None = None


class IdentityError(Exception):
    """Raised when a request carries no trustworthy user identity.

    Surfaced as HTTP 401 at the transport edge.
    """
