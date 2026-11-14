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
    # docs/api/invoice-rest.openapi.yaml in the apex repo. apex_api_key and
    # apex_wealth_api_key are DEPRECATED and ignored (kept only so existing .env
    # files still parse); per-user keys, and later Clerk act-as via
    # APEX_GATEWAY_API_KEY, replace them.
    apex_base_url: str = "http://localhost:3010"
    # System API key (Apex consumer GATEWAY, minted with scripts/create-gateway-api-key.ts)
    # that lets the gateway call Apex as a Clerk user via X-Act-As-Subject. Unset
    # disables act-as. Secret: never log it.
    apex_gateway_api_key: str | None = None
    apex_api_key: str | None = None
    # Wealth endpoints need a key for a PERSONAL workspace. DEPRECATED and ignored.
    apex_wealth_api_key: str | None = None

    token_encryption_key: str

    # Compatibility-only inputs: legacy keys/dev bypass are ignored by authentication.
    gateway_shared_secret: str = ""

    # Allowed MCP transport origin (not an authentication mechanism).
    trusted_open_webui_origin: str
    session_secret: str

    # Clerk OAuth is mandatory at application startup.
    clerk_issuer: str | None = None
    clerk_publishable_key: str | None = None
    clerk_backend_api_key: SecretStr | None = None
    clerk_session_audience: str | None = None
    clerk_browser_client_id: str | None = None
    clerk_browser_client_secret: str | None = None
    clerk_link_client_id: str | None = None
    clerk_link_client_secret: str | None = None
    clerk_link_scope: str = "openid profile mcp:tools"

    # Deprecated and ignored; development also requires OAuth.
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
