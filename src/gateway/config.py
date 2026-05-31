"""Application configuration loaded from the environment.

A single cached ``Settings`` instance is the source of truth for every
externally configured value. See ``.env.example`` for the full list.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed environment configuration."""

    database_url: str
    base_url: str = "http://localhost:8000"

    google_client_id: str
    google_client_secret: str

    token_encryption_key: str

    # Shared secret proving a request originates from the trusted Open WebUI
    # deployment. Open WebUI sends it as the ``X-Gateway-Auth`` header; the public
    # reverse proxy strips that header, so only the trusted (loopback) path can
    # present it. This is the primary gate for header-based identity.
    gateway_shared_secret: str

    # Defense-in-depth only: the expected Open WebUI origin. No longer the trust
    # gate (a client can spoof Origin); retained for optional logging/checks.
    trusted_open_webui_origin: str
    session_secret: str

    # Local-dev escape hatch: trust identity headers from any origin.
    dev_trust_all_origins: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def google_redirect_uri(self) -> str:
        """The OAuth redirect URI, derived from ``base_url``."""
        return f"{self.base_url.rstrip('/')}/oauth/google/callback"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached settings instance."""
    return Settings()  # type: ignore[call-arg]
