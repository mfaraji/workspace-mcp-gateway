"""OAuth client tool grants; retire static client credentials."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_client_grants"
down_revision = "0004_oauth_identity_links"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "client_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("issuer", sa.String(512), nullable=False),
        sa.Column("client_id", sa.String(255), nullable=False),
        sa.Column("tools", postgresql.JSONB(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("user_id", "issuer", "client_id"),
    )
    op.create_index("ix_client_grants_user_id", "client_grants", ["user_id"])
    op.execute("UPDATE client_tokens SET revoked_at = CURRENT_TIMESTAMP WHERE revoked_at IS NULL")


def downgrade():
    op.drop_table("client_grants")
    # Static tokens remain revoked intentionally.
