"""SQLAlchemy ORM models for the gateway data store.

Tables mirror the spec data model: users, provider connections, encrypted
provider tokens, and a tool audit log. Tokens are stored as ciphertext bytes;
see :mod:`gateway.crypto.tokens`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gateway.db.base import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class TimestampMixin:
    """created_at / updated_at columns managed by the database."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class User(Base, TimestampMixin):
    """An Open WebUI user, keyed by their external (Open WebUI) identity."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    external_user_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    connections: Mapped[list[ProviderConnection]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    client_tokens: Mapped[list[ClientToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class ProviderConnection(Base, TimestampMixin):
    """A user's connection to one provider account (e.g. one Google account)."""

    __tablename__ = "provider_connections"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", "provider_account_id", name="provider_account"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(64))
    provider_account_id: Mapped[str] = mapped_column(String(255))
    provider_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    connection_metadata: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="active")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="connections")
    token: Mapped[ProviderToken | None] = relationship(
        back_populates="connection", uselist=False, cascade="all, delete-orphan"
    )
    telegram_destinations: Mapped[list[TelegramDestination]] = relationship(
        back_populates="connection", cascade="all, delete-orphan"
    )


class ProviderToken(Base, TimestampMixin):
    """Encrypted OAuth tokens for a single provider connection (1:1)."""

    __tablename__ = "provider_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("provider_connections.id", ondelete="CASCADE"),
        unique=True,
    )
    encrypted_access_token: Mapped[bytes] = mapped_column(LargeBinary)
    encrypted_refresh_token: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    connection: Mapped[ProviderConnection] = relationship(back_populates="token")


class ClientToken(Base, TimestampMixin):
    """A static bearer token authenticating one user from a native MCP client.

    Used by clients (Cursor, Claude Desktop) that can't ride the on-host Open
    WebUI header path. Only the SHA-256 hash of the token is stored; the plaintext
    is shown once at creation. Revoke by setting ``revoked_at``.
    """

    __tablename__ = "client_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(128))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_prefix: Mapped[str] = mapped_column(String(16))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="client_tokens")


class OAuthIdentityLink(Base, TimestampMixin):
    """A verified OAuth subject bound to an existing Open WebUI user."""

    __tablename__ = "oauth_identity_links"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="oauth_identity_subject"),
        UniqueConstraint("user_id", "issuer", name="oauth_identity_user_issuer"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    issuer: Mapped[str] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(String(255))
    user: Mapped[User] = relationship()


class OAuthLinkIntent(Base):
    """Short-lived, single-use account-link flow state; no tokens are stored."""

    __tablename__ = "oauth_link_intents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    code_verifier: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user: Mapped[User] = relationship()


class ToolAuditLog(Base):
    """One row per tool invocation. Never stores secrets or raw content."""

    __tablename__ = "tool_audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(64))
    tool_name: Mapped[str] = mapped_column(String(128))
    # How the caller authenticated: "header", "token", or "oauth".
    auth_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_summary: Mapped[str | None] = mapped_column(String, nullable=True)
    result_status: Mapped[str] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class TelegramDestination(Base, TimestampMixin):
    """A channel that one user's connected Telegram bot may publish to."""

    __tablename__ = "telegram_destinations"
    __table_args__ = (
        UniqueConstraint("connection_id", "alias", name="telegram_destination_alias"),
        UniqueConstraint("connection_id", "chat_id", name="telegram_destination_chat"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("provider_connections.id", ondelete="CASCADE"),
        index=True,
    )
    alias: Mapped[str] = mapped_column(String(64))
    chat_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    connection: Mapped[ProviderConnection] = relationship(back_populates="telegram_destinations")
    deliveries: Mapped[list[TelegramDelivery]] = relationship(
        back_populates="destination", cascade="all, delete-orphan"
    )


class TelegramDelivery(Base, TimestampMixin):
    """Idempotency and outcome ledger for Telegram channel publications."""

    __tablename__ = "telegram_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    destination_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("telegram_destinations.id", ondelete="CASCADE"),
        index=True,
    )
    execution_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    telegram_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    destination: Mapped[TelegramDestination] = relationship(back_populates="deliveries")
