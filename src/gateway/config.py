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

    # Apex invoicing/timesheet/wealth API (see docs/api/invoice-rest.openapi.yaml in the
    # apex repo). There are no per-user keys: the gateway signs short-lived, Apex-only
    # tokens for the signed-in Clerk user (gateway.oauth.downstream).
    apex_base_url: str = "http://localhost:3010"

    # Downstream (Apex) tokens: after verifying the user's Clerk token the gateway mints
    # its own short-lived, Apex-only JWT (see gateway.oauth.downstream). Unencrypted PEM
    # for an EC P-256 or RSA>=2048 key; the public half is served at
    # /.well-known/apex-jwks.json. Secret: never log it.
    gateway_signing_key: SecretStr | None = None
    apex_mcp_audience: str = "apex-mcp-api"
    apex_token_ttl_seconds: int = 120

    # Only Gmail messages carrying this label are visible to the Gmail tools.
    gmail_access_label: str = "agent-access"
    # Label applied by the Gmail ``apply_sensitive_*_label`` tools (created on demand).
    gmail_sensitive_label: str = "Sensitive"
    # MCP Apps: serve interactive ``ui://`` widgets in chat. Off by default; tools then
    # return plain results with no widget metadata. See docs/mcp-apps.md.
    mcp_apps_enabled: bool = False

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
    # OAuth clients that identify themselves with a client-ID metadata document (the
    # client_id is an https URL, e.g. Claude Code) are trusted only from these hosts
    # (comma-separated). The consent page fetches the document to validate the redirect.
    cimd_allowed_hosts: str = "claude.ai"

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
    # Tool-call audit log (one document per call, expiring after ``audit_retention_days``).
    capella_audit_scope: str = "audit"
    capella_audit_collection: str = "tool_calls"
    audit_retention_days: int = 30
    # How long a human has to answer an in-chat confirmation question.
    confirmation_token_ttl_seconds: int = 600
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
