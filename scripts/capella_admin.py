"""Operator-only Capella helper: connection check, index setup, JSON export.

This is deliberately NOT exposed as an MCP tool — the model never gets
backup or database-administration capabilities. Run it on the gateway host
with the same environment (.env) the gateway uses:

    uv run python scripts/capella_admin.py check
    uv run python scripts/capella_admin.py create-indexes        # needs Query Manage Index
    uv run python scripts/capella_admin.py export --out recipes.jsonl [--owner <user-uuid>]

The export writes one JSON object per line: {"key": ..., "doc": {...}}.
It contains full recipe contents — store it like any other personal backup.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from gateway.config import get_settings
from gateway.storage.capella import errors
from gateway.storage.capella.client import CapellaClient
from gateway.storage.capella.repository import OwnedDocumentRepository

INDEX_NAME = "idx_recipes_owner_updated"
EXPORT_BATCH = 500


def _repo(client: CapellaClient) -> OwnedDocumentRepository:
    settings = client.settings
    return OwnedDocumentRepository(
        client,
        scope=settings.capella_recipes_scope,
        collection=settings.capella_recipes_collection,
        doc_type="recipe",
    )


def cmd_check(client: CapellaClient, _args: argparse.Namespace) -> int:
    repo = _repo(client)
    client.cluster()
    print(f"connected; keyspace {repo.keyspace}")
    probe = uuid.uuid4()
    try:
        repo.get(probe, uuid.uuid4())
    except errors.DocumentNotFound:
        print("KV access ok (collection reachable)")
    repo.query_owned(select="d.id", order_by="d.updated_at DESC, d.id", owner=probe, limit=1)
    print("query ok (owner index usable)")
    return 0


def cmd_create_indexes(client: CapellaClient, _args: argparse.Namespace) -> int:
    repo = _repo(client)
    statement = (
        f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
        f"ON {repo.keyspace}(owner_user_id, updated_at DESC, id)"
    )
    client.query(statement, {})
    print(f"index {INDEX_NAME} ensured on {repo.keyspace}")
    return 0


def cmd_export(client: CapellaClient, args: argparse.Namespace) -> int:
    repo = _repo(client)
    out = Path(args.out)
    count = 0
    with out.open("w", encoding="utf-8") as fh:
        offset = 0
        while True:
            if args.owner:
                rows = repo.query_owned(
                    select="META(d).id AS `key`, d AS doc",
                    order_by="d.updated_at DESC, d.id",
                    owner=uuid.UUID(args.owner),
                    limit=100,
                    offset=offset,
                )
                batch = 100
            else:
                # All users; still index-backed via the leading owner_user_id key.
                rows = client.query(
                    f"SELECT META(d).id AS `key`, d AS doc FROM {repo.keyspace} AS d "
                    "WHERE d.owner_user_id IS NOT MISSING "
                    "ORDER BY d.owner_user_id, d.updated_at DESC, d.id "
                    "LIMIT $limit OFFSET $offset",
                    {"limit": EXPORT_BATCH, "offset": offset},
                )
                batch = EXPORT_BATCH
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += len(rows)
            if len(rows) < batch:
                break
            offset += batch
    print(f"exported {count} document(s) to {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="verify connection, collection access and index")
    sub.add_parser("create-indexes", help="create the recipes index (idempotent)")
    export = sub.add_parser("export", help="export recipes as JSON lines")
    export.add_argument("--out", required=True, help="output .jsonl path")
    export.add_argument("--owner", help="only this gateway user UUID")
    args = parser.parse_args(argv)

    client = CapellaClient(get_settings())
    handler = {
        "check": cmd_check,
        "create-indexes": cmd_create_indexes,
        "export": cmd_export,
    }[args.command]
    try:
        return handler(client, args)
    except errors.StorageError as exc:
        print(f"error [{exc.error_code}]: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
