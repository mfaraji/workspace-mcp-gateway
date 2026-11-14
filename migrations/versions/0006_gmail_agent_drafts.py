"""Track Gmail drafts created by the gateway."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006_gmail_agent_drafts"
down_revision = "0005_client_grants"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "gmail_agent_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("google_account_id", sa.String(255), nullable=False),
        sa.Column("draft_id", sa.String(255), nullable=False),
        sa.Column("thread_id", sa.String(255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("user_id", "draft_id", name="gmail_agent_draft"),
    )
    op.create_index("ix_gmail_agent_drafts_user_id", "gmail_agent_drafts", ["user_id"])


def downgrade():
    op.drop_table("gmail_agent_drafts")
