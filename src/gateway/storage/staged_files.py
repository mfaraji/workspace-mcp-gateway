"""Short-lived encrypted file handles so bytes move between tools, not through the model.

``stage`` stores a file for one user and returns an opaque handle (``file_ref``);
``take`` returns and deletes it, but only for the owning user and before it expires.
Content is Fernet-encrypted at rest and is never logged or audited.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from gateway.crypto.tokens import get_cipher
from gateway.db.models import StagedFile, UsedUploadTicket

MAX_FILE_BYTES = 10 * 1024 * 1024  # matches the Apex receipt upload limit
STAGED_TTL = timedelta(minutes=15)


@dataclass(frozen=True)
class StagedContent:
    filename: str
    mime_type: str
    data: bytes
    sha256: str


def _now() -> datetime:
    return datetime.now(UTC)


def purge_expired(session: Session) -> None:
    now = _now()
    session.execute(delete(StagedFile).where(StagedFile.expires_at < now))
    session.execute(delete(UsedUploadTicket).where(UsedUploadTicket.expires_at < now))


def stage(
    session: Session,
    user_id: uuid.UUID,
    *,
    filename: str,
    mime_type: str,
    data: bytes,
    source: str,
) -> tuple[str, datetime]:
    """Store ``data`` for ``user_id``; returns ``(file_ref, expires_at)``."""
    from gateway.providers.registry import ToolError

    if len(data) > MAX_FILE_BYTES:
        raise ToolError("too_large", "file exceeds the 10 MB limit")
    purge_expired(session)
    expires_at = _now() + STAGED_TTL
    row = StagedFile(
        user_id=user_id,
        filename=filename[:255],
        mime_type=mime_type[:127],
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        source=source,
        content=get_cipher().encrypt(base64.b64encode(data).decode()),
        expires_at=expires_at,
    )
    session.add(row)
    session.flush()
    return str(row.id), expires_at


def take(session: Session, user_id: uuid.UUID, file_ref: str) -> StagedContent:
    """Return and delete the staged file; unknown, foreign or expired handles are ``not_found``."""
    from gateway.providers.registry import ToolError

    try:
        ref = uuid.UUID(file_ref)
    except ValueError:
        raise ToolError("not_found", "file_ref not found or expired") from None
    row = session.scalars(select(StagedFile).where(StagedFile.id == ref).with_for_update()).first()
    if row is None or row.user_id != user_id or row.expires_at < _now():
        raise ToolError("not_found", "file_ref not found or expired")
    data = base64.b64decode(get_cipher().decrypt(row.content))
    result = StagedContent(row.filename, row.mime_type, data, row.sha256)
    session.delete(row)
    session.flush()
    return result


def consume_ticket(session: Session, jti: str, expires_at: datetime) -> bool:
    """Mark an upload ticket used; ``False`` when it was already consumed."""
    from sqlalchemy.dialects.postgresql import insert

    purge_expired(session)
    result = session.execute(
        insert(UsedUploadTicket)
        .values(jti=jti, expires_at=expires_at)
        .on_conflict_do_nothing(index_elements=["jti"])
    )
    return result.rowcount == 1
