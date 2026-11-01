"""Shared document envelope and pagination models for owner-scoped storage."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string (millisecond precision, ``Z``)."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class StoredDocument(BaseModel):
    """Fields every owner-scoped document carries. Never accepted from clients."""

    id: str
    owner_user_id: str
    schema_version: int = 1
    created_at: str
    updated_at: str


class Page(BaseModel):
    """One page of results with offset pagination."""

    items: list[dict[str, Any]]
    limit: int
    offset: int
    next_offset: int | None = Field(
        None, description="Pass as `offset` to fetch the next page; null when done."
    )

    @classmethod
    def from_rows(cls, rows: list[dict[str, Any]], *, limit: int, offset: int) -> Page:
        """Build a page from ``limit + 1`` fetched rows (the extra row signals more)."""
        has_more = len(rows) > limit
        return cls(
            items=rows[:limit],
            limit=limit,
            offset=offset,
            next_offset=offset + limit if has_more else None,
        )
