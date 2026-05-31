"""Symmetric encryption for provider tokens at rest.

Uses Fernet (AES-128-CBC + HMAC) with the key from ``TOKEN_ENCRYPTION_KEY``.
The wrapper returns to a ``MultiFernet`` shape so key rotation can be added later
without touching callers.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet

from gateway.config import get_settings


def hash_token(token: str) -> str:
    """Return the hex SHA-256 of a bearer token, for storage and lookup.

    Bearer tokens carry enough entropy that a fast unsalted hash is sufficient;
    we never store the plaintext.
    """
    return hashlib.sha256(token.encode()).hexdigest()


class TokenCipher:
    """Encrypts/decrypts token strings to/from ciphertext bytes."""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, token: bytes) -> str:
        return self._fernet.decrypt(token).decode()


@lru_cache
def get_cipher() -> TokenCipher:
    """Return the process-wide token cipher."""
    return TokenCipher(get_settings().token_encryption_key)
