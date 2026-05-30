"""Identity value objects."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AuthenticatedUser:
    """A verified Open WebUI user identity for the current request."""

    external_user_id: str
    email: str | None = None
    display_name: str | None = None


class IdentityError(Exception):
    """Raised when a request carries no trustworthy user identity.

    Surfaced as HTTP 401 at the transport edge.
    """
