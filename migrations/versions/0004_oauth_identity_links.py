"""OAuth identity links and short-lived single-use link intents.

Revision ID: 0004_oauth_identity_links
Revises: 0003_telegram_destinations
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_oauth_identity_links"
down_revision = "0003_telegram_destinations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth_identity_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("issuer", sa.String(512), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("issuer", "subject", name="oauth_identity_subject"),
        sa.UniqueConstraint("user_id", "issuer", name="oauth_identity_user_issuer"),
    )
    op.create_index("ix_oauth_identity_links_user_id", "oauth_identity_links", ["user_id"])
    op.create_table(
        "oauth_link_intents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("code_verifier", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_oauth_link_intents_user_id", "oauth_link_intents", ["user_id"])
    op.create_index(
        "ix_oauth_link_intents_state_hash", "oauth_link_intents", ["state_hash"], unique=True
    )


def downgrade() -> None:
    op.drop_table("oauth_link_intents")
    op.drop_table("oauth_identity_links")
