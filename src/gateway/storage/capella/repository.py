"""Owner-scoped document repository: the reusable isolation layer.

Every storage-backed provider (recipes today; notes, knowledge, finance later)
goes through :class:`OwnedDocumentRepository`, which enforces per-user
isolation in two independent ways:

1. **Key scoping** — document keys embed the owner:
   ``user::<owner_uuid>::<doc_type>::<doc_uuid>``. A caller can only ever build
   keys under their own ``owner`` (always ``CallContext.user_id``), so another
   user's document is simply not addressable.
2. **Ownership verification** — every read checks the stored
   ``owner_user_id`` and reports a mismatch as *not found* (never "forbidden"),
   so nothing leaks about other users' documents. Queries always carry a
   ``d.owner_user_id = $owner`` predicate injected here, not by callers.

SQL++ is composed only from code constants supplied by provider modules; all
values travel as named parameters. Nothing here accepts client-supplied SQL++.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from gateway.storage.capella import errors
from gateway.storage.capella.client import CapellaClient, translate_exception

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-%]{0,250}$")
_DOC_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

MAX_PAGE_SIZE = 100
MAX_OFFSET = 10_000


def _validate_identifier(value: str, what: str) -> str:
    if not _IDENTIFIER.match(value):
        raise errors.StorageMisconfigured(f"invalid Capella {what} name")
    return value


def parse_uuid(value: str | uuid.UUID) -> uuid.UUID:
    """Parse a document/owner id; anything that isn't a UUID is not found."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise errors.DocumentNotFound() from None


class OwnedDocumentRepository:
    """CRUD + owner-scoped SQL++ over one ``bucket.scope.collection``."""

    def __init__(
        self, client: CapellaClient, *, scope: str, collection: str, doc_type: str
    ) -> None:
        if not _DOC_TYPE.match(doc_type):
            raise ValueError("doc_type must be a short lowercase identifier")
        self.client = client
        self.scope = _validate_identifier(scope, "scope")
        self.collection_name = _validate_identifier(collection, "collection")
        self.doc_type = doc_type

    # -- keys -----------------------------------------------------------------

    def key(self, owner: uuid.UUID | str, doc_id: uuid.UUID | str) -> str:
        """Build the owner-scoped document key (both ids must be UUIDs)."""
        return f"user::{parse_uuid(owner)}::{self.doc_type}::{parse_uuid(doc_id)}"

    @property
    def keyspace(self) -> str:
        bucket = _validate_identifier(self.client.bucket_name, "bucket")
        return f"`{bucket}`.`{self.scope}`.`{self.collection_name}`"

    def _collection(self) -> Any:
        return self.client.collection(self.scope, self.collection_name)

    # -- KV -------------------------------------------------------------------

    def get(self, owner: uuid.UUID, doc_id: uuid.UUID | str) -> tuple[dict[str, Any], int]:
        """Return ``(document, cas)``; missing or not owned -> ``DocumentNotFound``."""
        key = self.key(owner, doc_id)
        collection = self._collection()
        try:
            result = collection.get(key)
        except Exception as exc:
            raise translate_exception(exc) from None
        doc = result.content_as[dict]
        if not isinstance(doc, dict) or doc.get("owner_user_id") != str(owner):
            raise errors.DocumentNotFound()
        return doc, int(result.cas)

    def insert(self, owner: uuid.UUID, doc_id: uuid.UUID | str, doc: dict[str, Any]) -> int:
        """Create a document (fails with ``DocumentAlreadyExists``); return its CAS."""
        self._assert_owned(owner, doc_id, doc)
        key = self.key(owner, doc_id)
        collection = self._collection()
        try:
            result = collection.insert(key, doc)
        except Exception as exc:
            raise translate_exception(exc, write=True) from None
        return int(result.cas)

    def replace(
        self, owner: uuid.UUID, doc_id: uuid.UUID | str, doc: dict[str, Any], cas: int
    ) -> int:
        """Replace a document only if its CAS still matches; return the new CAS."""
        from couchbase.options import ReplaceOptions

        self._assert_owned(owner, doc_id, doc)
        key = self.key(owner, doc_id)
        collection = self._collection()
        try:
            result = collection.replace(key, doc, ReplaceOptions(cas=int(cas)))
        except Exception as exc:
            raise translate_exception(exc, write=True) from None
        return int(result.cas)

    def remove(self, owner: uuid.UUID, doc_id: uuid.UUID | str, cas: int | None = None) -> None:
        """Delete an owned document (optionally CAS-checked)."""
        from couchbase.options import RemoveOptions

        key = self.key(owner, doc_id)
        collection = self._collection()
        try:
            if cas is None:
                collection.remove(key)
            else:
                collection.remove(key, RemoveOptions(cas=int(cas)))
        except Exception as exc:
            raise translate_exception(exc, write=True) from None

    def _assert_owned(self, owner: uuid.UUID, doc_id: uuid.UUID | str, doc: dict) -> None:
        # Defense in depth: the body must agree with the key it is stored under.
        if doc.get("owner_user_id") != str(owner) or doc.get("id") != str(parse_uuid(doc_id)):
            raise ValueError("document owner/id must match the key it is stored under")

    # -- SQL++ ----------------------------------------------------------------

    def build_owned_query(
        self,
        *,
        select: str,
        order_by: str,
        where: str | None = None,
        params: dict[str, Any] | None = None,
        owner: uuid.UUID,
        limit: int,
        offset: int = 0,
    ) -> tuple[str, dict[str, Any]]:
        """Compose an owner-scoped, paginated statement and its named parameters.

        ``select``/``where``/``order_by`` are code constants referring to the
        document alias ``d``; ``owner``, ``limit`` and ``offset`` are reserved
        parameter names and cannot be overridden by ``params``.
        """
        params = dict(params or {})
        reserved = {"owner", "limit", "offset"} & params.keys()
        if reserved:
            raise ValueError(f"reserved query parameter(s): {sorted(reserved)}")
        limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        offset = max(0, min(int(offset), MAX_OFFSET))
        clause = "d.owner_user_id = $owner"
        if where:
            clause += f" AND ({where})"
        statement = (
            f"SELECT {select} FROM {self.keyspace} AS d "
            f"WHERE {clause} ORDER BY {order_by} LIMIT $limit OFFSET $offset"
        )
        params.update({"owner": str(parse_uuid(owner)), "limit": limit, "offset": offset})
        return statement, params

    def query_owned(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Run :meth:`build_owned_query` and return its rows."""
        statement, params = self.build_owned_query(**kwargs)
        return self.client.query(statement, params)
