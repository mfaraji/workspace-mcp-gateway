"""secure Telegram connections, destinations, and delivery ledger

Revision ID: 0003_telegram_destinations
Revises: 0002_client_tokens
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_telegram_destinations"
down_revision = "0002_client_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provider_connections",
        sa.Column("connection_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.create_table(
        "telegram_destinations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("alias", sa.String(length=64), nullable=False),
        sa.Column("chat_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["connection_id"], ["provider_connections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "alias", name="telegram_destination_alias"),
        sa.UniqueConstraint("connection_id", "chat_id", name="telegram_destination_chat"),
    )
    op.create_index(
        "ix_telegram_destinations_connection_id", "telegram_destinations", ["connection_id"]
    )
    op.create_table(
        "telegram_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("destination_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_key", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("telegram_message_id", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["destination_id"], ["telegram_destinations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_telegram_deliveries_user_id", "telegram_deliveries", ["user_id"])
    op.create_index(
        "ix_telegram_deliveries_destination_id", "telegram_deliveries", ["destination_id"]
    )
    op.create_index(
        "ix_telegram_deliveries_execution_key",
        "telegram_deliveries",
        ["execution_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("telegram_deliveries")
    op.drop_table("telegram_destinations")
    op.drop_column("provider_connections", "connection_metadata")
