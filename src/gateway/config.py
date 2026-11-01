"""Application configuration loaded from the environment.

A single cached ``Settings`` instance is the source of truth for every
externally configured value. See ``.env.example`` for the full list.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed environment configuration."""

    database_url: str
    base_url: str = "http://localhost:8000"

    google_client_id: str
    google_client_secret: str

    # Apex invoicing/timesheet API. Each user stores their own key (via the
    # apex_set_api_key tool or POST /connectors/apex/apikey) — see
    # docs/api/invoice-rest.openapi.yaml in the apex repo. apex_api_key is an
    # optional deploy-wide fallback for users who haven't set a personal key
    # yet; it is not required once every user has one.
    apex_base_url: str = "http://localhost:3010"
    apex_api_key: str | None = None
    # Wealth endpoints need a key for a PERSONAL workspace; optional deploy-wide fallback.
    apex_wealth_api_key: str | None = None

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

    # Clerk OAuth authorization server. Unset means the existing header/static
    # token paths remain the only authentication methods.
    clerk_issuer: str | None = None
    clerk_link_client_id: str | None = None
    clerk_link_client_secret: str | None = None
    clerk_link_scope: str = "openid profile mcp:tools"

    # Local-dev escape hatch: trust identity headers from any origin.
    dev_trust_all_origins: bool = False

    # Couchbase Capella document storage (optional). Unset means storage-backed
    # connectors (recipes) report ``storage_not_configured``; every other
    # connector is unaffected. One cluster + one bucket, one scope per domain.
    # The connection string must use TLS (``couchbases://``).
    capella_connection_string: str | None = None
    capella_username: str | None = None
    capella_password: SecretStr | None = None
    capella_bucket: str = "personal"
    capella_recipes_scope: str = "recipes"
    capella_recipes_collection: str = "recipes"
    capella_connect_timeout_seconds: float = 10.0
    capella_kv_timeout_seconds: float = 5.0
    capella_query_timeout_seconds: float = 15.0

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

    @property
    def capella_configured(self) -> bool:
        """Whether every credential needed to reach Capella is present."""
        return bool(
            self.capella_connection_string
            and self.capella_username
            and self.capella_password
            and self.capella_password.get_secret_value()
        )

    @property
    def clerk_link_redirect_uri(self) -> str:
        """Callback for the gateway's confidential account-linking OAuth client."""
        return f"{self.base_url.rstrip('/')}/oauth/clerk/link/callback"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached settings instance."""
    return Settings()  # type: ignore[call-arg]
