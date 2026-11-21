"""Remove stored Apex API keys and default workspaces.

Apex access no longer uses per-user keys or a stored default workspace: the gateway
mints a short-lived Apex-only token per call for the signed-in Clerk user and the
workspace is passed on each tool call. The rows are secrets/obsolete state, so they
are deleted (not archived). Irreversible by design.
"""

from alembic import op

revision = "0007_drop_apex_credentials"
down_revision = "0006_gmail_agent_drafts"
branch_labels = None
depends_on = None

_PROVIDERS = "('apex', 'apex_wealth')"


def upgrade():
    op.execute(
        "DELETE FROM provider_tokens WHERE connection_id IN "
        f"(SELECT id FROM provider_connections WHERE provider IN {_PROVIDERS})"
    )
    op.execute(f"DELETE FROM provider_connections WHERE provider IN {_PROVIDERS}")


def downgrade():
    # Deleted credentials cannot be restored.
    pass
