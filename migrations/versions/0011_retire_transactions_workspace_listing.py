"""Retire apex_transactions_list_workspaces; spending tools use apex_list_workspaces."""

from alembic import op

revision = "0011_retire_txn_listing"
down_revision = "0010_staged_files"
branch_labels = None
depends_on = None

OLD = "apex_transactions_list_workspaces"
NEW = "apex_list_workspaces"


def upgrade():
    # A client that was allowed the old listing keeps being able to list workspaces.
    op.execute(
        f"""
        UPDATE client_grants
        SET tools = (
            SELECT COALESCE(jsonb_agg(DISTINCT t ORDER BY t), '[]'::jsonb)
            FROM (
                SELECT jsonb_array_elements_text(tools) AS t
                UNION SELECT '{NEW}'
            ) names
            WHERE t <> '{OLD}'
        )
        WHERE tools ? '{OLD}'
        """
    )
    op.execute(f"DELETE FROM user_tool_settings WHERE tool_name = '{OLD}'")


def downgrade():
    """Not reversible: the old tool is gone, so there is nothing to grant."""
