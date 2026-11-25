"""Signed, single-use, short-lived tickets for uploading a local file to Apex.

The ticket is the only credential of ``POST /uploads/apex-receipt`` (sent as a bearer
token, never in the URL so it stays out of access logs). It binds the user, the OAuth
client and the target expense; single use is enforced through ``used_upload_tickets``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from gateway.config import Settings

TICKET_TTL_SECONDS = 600
_SALT = "apex-receipt-upload"


@dataclass(frozen=True)
class UploadTicket:
    user_id: uuid.UUID
    clerk_subject: str
    oauth_client_id: str | None
    expense_id: str
    workspace_id: str
    jti: str


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt=_SALT)


def mint_ticket(
    settings: Settings,
    *,
    user_id: uuid.UUID,
    clerk_subject: str,
    oauth_client_id: str | None,
    expense_id: str,
    workspace_id: str,
) -> str:
    return _serializer(settings).dumps(
        {
            "uid": str(user_id),
            "sub": clerk_subject,
            "cid": oauth_client_id,
            "eid": expense_id,
            "wsid": workspace_id,
            "jti": uuid.uuid4().hex,
        }
    )


def verify_ticket(settings: Settings, token: str) -> UploadTicket | None:
    """Return the ticket's claims, or ``None`` when it is invalid or expired."""
    try:
        p = _serializer(settings).loads(token, max_age=TICKET_TTL_SECONDS)
        return UploadTicket(
            uuid.UUID(p["uid"]), p["sub"], p.get("cid"), p["eid"], p["wsid"], p["jti"]
        )
    except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError):
        return None
