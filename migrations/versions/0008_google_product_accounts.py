"""Per-product Google connections with several accounts per product.

Each ``provider_connections`` row for Google now serves exactly one product
(calendar / drive / tasks / gmail) and one Google account; a product may have
several accounts, one of which is the default. Existing single-row Google
connections are split into one row per product whose scopes they already hold,
copying the encrypted token bytes verbatim, so nobody has to re-authorize.
"""

import json
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_google_product_accounts"
down_revision = "0007_drop_apex_credentials"
branch_labels = None
depends_on = None

_OPENID = ("openid", "email", "profile")
# Hard-coded on purpose: migrations must not depend on application code.
_G = "https://www.googleapis.com/auth/"
_PRODUCT_SCOPES = {
    "calendar": (_G + "calendar.readonly", _G + "calendar.events"),
    "drive": (_G + "drive.metadata.readonly", _G + "drive.readonly"),
    "tasks": (_G + "tasks.readonly", _G + "tasks"),
    "gmail": (_G + "gmail.readonly", _G + "gmail.compose", _G + "gmail.labels"),
}


def upgrade():
    op.add_column(
        "provider_connections",
        sa.Column("product", sa.String(32), nullable=False, server_default=""),
    )
    op.add_column(
        "provider_connections",
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.drop_constraint("provider_account", "provider_connections", type_="unique")
    op.create_unique_constraint(
        "provider_account",
        "provider_connections",
        ["user_id", "provider", "product", "provider_account_id"],
    )
    op.create_index(
        "uq_provider_connection_default",
        "provider_connections",
        ["user_id", "provider", "product"],
        unique=True,
        postgresql_where=sa.text("is_default AND status = 'active'"),
    )

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT c.id, c.user_id, c.provider_account_id, c.provider_email, c.scopes, "
            "c.connection_metadata, c.status, c.last_used_at, c.created_at, c.updated_at "
            "FROM provider_connections c WHERE c.provider = 'google' AND c.product = ''"
        )
    ).mappings().all()
    connections = sa.table(
        "provider_connections",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("user_id", postgresql.UUID(as_uuid=True)),
        sa.column("provider", sa.String),
        sa.column("product", sa.String),
        sa.column("is_default", sa.Boolean),
        sa.column("provider_account_id", sa.String),
        sa.column("provider_email", sa.String),
        sa.column("scopes", postgresql.JSONB),
        sa.column("connection_metadata", postgresql.JSONB),
        sa.column("status", sa.String),
        sa.column("last_used_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    tokens = sa.table(
        "provider_tokens",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("connection_id", postgresql.UUID(as_uuid=True)),
        sa.column("encrypted_access_token", sa.LargeBinary),
        sa.column("encrypted_refresh_token", sa.LargeBinary),
        sa.column("expires_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    for row in rows:
        if row["status"] == "revoked":
            continue  # revoked rows carry no tokens; they are deleted below
        held = set(row["scopes"] or [])
        token = bind.execute(
            sa.text(
                "SELECT encrypted_access_token, encrypted_refresh_token, expires_at, "
                "created_at, updated_at FROM provider_tokens WHERE connection_id = :id"
            ),
            {"id": row["id"]},
        ).mappings().first()
        for product, scopes in _PRODUCT_SCOPES.items():
            if not held.intersection(scopes):
                continue
            new_id = uuid.uuid4()
            keep = sorted(held.intersection(_OPENID + scopes))
            metadata = row["connection_metadata"] if product == "gmail" else {}
            bind.execute(
                connections.insert().values(
                    id=new_id,
                    user_id=row["user_id"],
                    provider="google",
                    product=product,
                    is_default=row["status"] == "active",
                    provider_account_id=row["provider_account_id"],
                    provider_email=row["provider_email"],
                    scopes=keep,
                    connection_metadata=metadata or {},
                    status=row["status"],
                    last_used_at=row["last_used_at"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            )
            if token is not None:
                bind.execute(
                    tokens.insert().values(
                        id=uuid.uuid4(),
                        connection_id=new_id,
                        encrypted_access_token=token["encrypted_access_token"],
                        encrypted_refresh_token=token["encrypted_refresh_token"],
                        expires_at=token["expires_at"],
                        created_at=token["created_at"],
                        updated_at=token["updated_at"],
                    )
                )
    # Drop the legacy shared rows (provider_tokens cascades on connection delete).
    op.execute(
        "DELETE FROM provider_tokens WHERE connection_id IN "
        "(SELECT id FROM provider_connections WHERE provider = 'google' AND product = '')"
    )
    op.execute("DELETE FROM provider_connections WHERE provider = 'google' AND product = ''")


def downgrade():
    """Best effort: collapse per-product rows back to one row per Google account.

    Scopes are unioned and one token (the most recently updated) is kept. Accounts
    beyond the first per user are dropped, because the old schema allowed only one
    active Google account.
    """
    bind = op.get_bind()
    op.execute(
        "DELETE FROM provider_tokens WHERE connection_id IN ("
        "SELECT id FROM provider_connections WHERE provider = 'google' AND status <> 'active')"
    )
    op.execute("DELETE FROM provider_connections WHERE provider = 'google' AND status <> 'active'")
    users = bind.execute(
        sa.text("SELECT DISTINCT user_id FROM provider_connections WHERE provider = 'google'")
    ).scalars().all()
    for user_id in users:
        rows = bind.execute(
            sa.text(
                "SELECT id, provider_account_id, scopes, is_default, updated_at "
                "FROM provider_connections WHERE provider = 'google' AND user_id = :u "
                "ORDER BY is_default DESC, updated_at DESC"
            ),
            {"u": user_id},
        ).mappings().all()
        keep_account = rows[0]["provider_account_id"]
        mine = [r for r in rows if r["provider_account_id"] == keep_account]
        survivor = mine[0]["id"]
        union = sorted({s for r in mine for s in (r["scopes"] or [])})
        bind.execute(
            sa.text("UPDATE provider_connections SET scopes = CAST(:s AS jsonb) WHERE id = :id"),
            {"s": json.dumps(union), "id": survivor},
        )
        others = [r["id"] for r in rows if r["id"] != survivor]
        for oid in others:
            bind.execute(sa.text("DELETE FROM provider_tokens WHERE connection_id = :i"), {"i": oid})
            bind.execute(sa.text("DELETE FROM provider_connections WHERE id = :i"), {"i": oid})
    op.drop_index("uq_provider_connection_default", table_name="provider_connections")
    op.drop_constraint("provider_account", "provider_connections", type_="unique")
    op.create_unique_constraint(
        "provider_account",
        "provider_connections",
        ["user_id", "provider", "provider_account_id"],
    )
    op.drop_column("provider_connections", "is_default")
    op.drop_column("provider_connections", "product")
