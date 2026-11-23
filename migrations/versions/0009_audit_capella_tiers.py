"""Per-user tool risk tiers; tool audit moves to Capella (30-day expiry).

``tool_audit_log`` is dropped: audit documents now live in Capella with a
per-document expiry. The downgrade recreates the (empty) table.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_audit_capella_tiers"
down_revision = "0008_google_product_accounts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_tool_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("user_id", "tool_name", name="user_tool_setting"),
    )
    op.create_index("ix_user_tool_settings_user_id", "user_tool_settings", ["user_id"])

    op.drop_index("ix_tool_audit_log_created_at", table_name="tool_audit_log")
    op.drop_index("ix_tool_audit_log_user_id", table_name="tool_audit_log")
    op.drop_table("tool_audit_log")


def downgrade():
    op.create_table(
        "tool_audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("auth_source", sa.String(16), nullable=True),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("input_summary", sa.String(), nullable=True),
        sa.Column("result_status", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_tool_audit_log_user_id", "tool_audit_log", ["user_id"])
    op.create_index("ix_tool_audit_log_created_at", "tool_audit_log", ["created_at"])
    op.drop_index("ix_user_tool_settings_user_id", table_name="user_tool_settings")
    op.drop_table("user_tool_settings")
