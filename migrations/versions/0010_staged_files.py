"""Staged files (tool-to-tool handoff) and one-time upload tickets."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_staged_files"
down_revision = "0009_audit_capella_tiers"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "staged_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(127), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_staged_files_user_id", "staged_files", ["user_id"])
    op.create_index("ix_staged_files_expires_at", "staged_files", ["expires_at"])
    op.create_table(
        "used_upload_tickets",
        sa.Column("jti", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_used_upload_tickets_expires_at", "used_upload_tickets", ["expires_at"])


def downgrade():
    op.drop_table("used_upload_tickets")
    op.drop_table("staged_files")
