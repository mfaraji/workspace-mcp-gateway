"""One-time receipt upload endpoint for agents that hold the file locally.

``apex_upload_expense_receipt`` returns a signed ticket; the agent POSTs the file here
with ``Authorization: Bearer <ticket>``. The ticket is the only credential (single use,
10 minutes), is never logged, and the file is streamed on to the Apex expense inbox.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from gateway.audit.log import AuditRecord, record_audit, summarize_input
from gateway.config import get_settings
from gateway.db.engine import session_scope
from gateway.providers.apex.client import SCOPE_EXPENSES_WRITE, ApexCredentials, _mint
from gateway.providers.apex.expenses import _upload_to_expense
from gateway.providers.apex.tickets import verify_ticket
from gateway.providers.registry import ToolError
from gateway.storage.staged_files import MAX_FILE_BYTES, consume_ticket

router = APIRouter(tags=["apex-uploads"])

_TOOL = "apex_upload_expense_receipt"
_ALLOWED_MIME = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
}


def _error(code: str, detail: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"error": code, "detail": detail}, status_code=status, headers={"Cache-Control": "no-store"}
    )


@router.post("/uploads/apex-receipt", include_in_schema=False)
async def upload_apex_receipt(request: Request, file: UploadFile) -> JSONResponse:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    settings = get_settings()
    ticket = verify_ticket(settings, token.strip()) if scheme.lower() == "bearer" else None
    if ticket is None:
        return _error("unauthorized", "Missing, invalid or expired upload ticket.", 401)

    mime = (file.content_type or "").split(";")[0].strip().lower()
    if mime not in _ALLOWED_MIME:
        return _error("invalid_input", "Unsupported file type.", 415)
    data = await file.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        return _error("too_large", "File exceeds the 10 MB limit.", 413)
    if not data:
        return _error("invalid_input", "Empty file.", 400)

    try:
        with session_scope() as session:
            fresh = consume_ticket(session, ticket.jti, datetime.now(UTC) + timedelta(hours=1))
    except SQLAlchemyError:
        return _error("internal_error", "Backing store unavailable.", 503)
    if not fresh:
        return _error("unauthorized", "Upload ticket already used.", 401)

    started = time.monotonic()
    status, code = "ok", None
    try:
        token_str, jti = _mint(
            settings, ticket.clerk_subject, ticket.oauth_client_id, [SCOPE_EXPENSES_WRITE], None
        )
        result = _upload_to_expense(
            settings,
            ApexCredentials(token_str, {}, jti),
            ticket.expense_id,
            file.filename or "receipt",
            mime,
            data,
        )
    except ToolError as exc:
        status, code = "error", exc.error_code
        result = None
    record_audit(
        AuditRecord(
            user_id=ticket.user_id,
            provider="apex",
            tool_name=_TOOL,
            result_status=status,
            input_summary=summarize_input(_TOOL, {"expense_id": ticket.expense_id}),
            error_code=code,
            auth_source="upload_ticket",
            risk_tier="low",
            duration_ms=int((time.monotonic() - started) * 1000),
            oauth_client_id=ticket.oauth_client_id,
        )
    )
    if status == "error":
        http = {"not_found": 404, "forbidden": 403, "unauthorized": 401}.get(code or "", 502)
        return _error(code or "provider_error", "Apex rejected the upload.", http)
    return JSONResponse(
        {"status": "uploaded", "receipt": result}, headers={"Cache-Control": "no-store"}
    )
