"""Exceptions shared across connector upstream-auth strategies."""

from __future__ import annotations


class ReauthRequired(Exception):
    """Raised when a connection's tokens are unusable and re-consent is needed."""


class AccountConflict(Exception):
    """Raised when a user tries to replace their one active upstream account."""
