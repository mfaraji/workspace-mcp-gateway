"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-30

Tables: users, provider_connections, provider_tokens, tool_audit_log.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_user_id", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(
        op.f("ix_users_external_user_id"), "users", ["external_user_id"], unique=True
    )

    op.create_table(
        "provider_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_account_id", sa.String(length=255), nullable=False),
        sa.Column("provider_email", sa.String(length=320), nullable=True),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_provider_connections_user_id_users"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_connections")),
        sa.UniqueConstraint(
            "user_id", "provider", "provider_account_id", name="provider_account"
        ),
    )
    op.create_index(
        op.f("ix_provider_connections_user_id"), "provider_connections", ["user_id"], unique=False
    )

    op.create_table(
        "provider_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("encrypted_access_token", sa.LargeBinary(), nullable=False),
        sa.Column("encrypted_refresh_token", sa.LargeBinary(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["provider_connections.id"],
            name=op.f("fk_provider_tokens_connection_id_provider_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_tokens")),
        sa.UniqueConstraint("connection_id", name=op.f("uq_provider_tokens_connection_id")),
    )

    op.create_table(
        "tool_audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("input_summary", sa.String(), nullable=True),
        sa.Column("result_status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_tool_audit_log_user_id_users"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_audit_log")),
    )
    op.create_index(
        op.f("ix_tool_audit_log_user_id"), "tool_audit_log", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_tool_audit_log_created_at"), "tool_audit_log", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_tool_audit_log_created_at"), table_name="tool_audit_log")
    op.drop_index(op.f("ix_tool_audit_log_user_id"), table_name="tool_audit_log")
    op.drop_table("tool_audit_log")
    op.drop_table("provider_tokens")
    op.drop_index(op.f("ix_provider_connections_user_id"), table_name="provider_connections")
    op.drop_table("provider_connections")
    op.drop_index(op.f("ix_users_external_user_id"), table_name="users")
    op.drop_table("users")
