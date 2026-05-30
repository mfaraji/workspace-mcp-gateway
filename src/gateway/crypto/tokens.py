"""Symmetric encryption for provider tokens at rest.

Uses Fernet (AES-128-CBC + HMAC) with the key from ``TOKEN_ENCRYPTION_KEY``.
The wrapper returns to a ``MultiFernet`` shape so key rotation can be added later
without touching callers.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet

from gateway.config import get_settings


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
