"""client tokens

Revision ID: 0002_client_tokens
Revises: 0001_initial
Create Date: 2026-05-31

Adds the client_tokens table: static per-user bearer tokens for native MCP
clients. Only the SHA-256 hash of each token is stored.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002_client_tokens"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "client_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_client_tokens_user_id_users"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_client_tokens")),
    )
    op.create_index(
        op.f("ix_client_tokens_user_id"), "client_tokens", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_client_tokens_token_hash"), "client_tokens", ["token_hash"], unique=True
    )

    # Record on each audit row how the caller authenticated.
    op.add_column(
        "tool_audit_log", sa.Column("auth_source", sa.String(length=16), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("tool_audit_log", "auth_source")
    op.drop_index(op.f("ix_client_tokens_token_hash"), table_name="client_tokens")
    op.drop_index(op.f("ix_client_tokens_user_id"), table_name="client_tokens")
    op.drop_table("client_tokens")
