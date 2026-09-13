"""Private Google Drive attachment API for the on-host Open WebUI backend."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.exc import SQLAlchemyError

from gateway.audit.log import write_audit
from gateway.config import get_settings
from gateway.db.engine import session_scope
from gateway.identity.models import AuthenticatedUser, IdentityError
from gateway.identity.resolver import get_or_create_user, resolve_identity
from gateway.oauth.google import build_start_url
from gateway.providers.google.connections import get_active_connection
from gateway.providers.google.drive.common import (
    DriveProviderError,
    build_scoped_drive_service,
    prepare_download,
    required_scopes_present,
    search_files,
)

router = APIRouter(prefix="/api/drive", tags=["drive-integration"])


def _authenticate(request: Request) -> tuple[AuthenticatedUser, uuid.UUID] | JSONResponse:
    settings = get_settings()
    try:
        auth = resolve_identity(dict(request.headers), settings)
    except IdentityError as exc:
        return JSONResponse(
            {"error": "unauthorized", "detail": str(exc)},
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )

    try:
        with session_scope() as session:
            user_id = get_or_create_user(session, auth).id
    except SQLAlchemyError:
        return JSONResponse(
            {"error": "internal_error", "detail": "Backing store unavailable."},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    return auth, user_id


def _audit(
    *,
    user_id: uuid.UUID,
    tool_name: str,
    request_id: str,
    input_summary: str,
    result_status: str,
    error_code: str | None,
    auth_source: str,
) -> None:
    """Best-effort route audit, isolated from the provider transaction."""
    try:
        with session_scope() as session:
            write_audit(
                session,
                user_id=user_id,
                provider="google",
                tool_name=tool_name,
                request_id=request_id,
                input_summary=input_summary,
                result_status=result_status,
                error_code=error_code,
                auth_source=auth_source,
            )
    except SQLAlchemyError:
        pass


def _error_response(
    error: DriveProviderError, *, external_user_id: str | None = None
) -> JSONResponse:
    body = {"error": error.code, "detail": str(error)}
    if error.code == "reauth_required" and external_user_id:
        body["authorization_url"] = build_start_url(
            get_settings(), external_user_id, product="drive"
        )
    return JSONResponse(
        body,
        status_code=error.status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/status")
def status(request: Request):
    """Return the caller's Drive connection state and product-scoped OAuth URL."""
    resolved = _authenticate(request)
    if isinstance(resolved, JSONResponse):
        return resolved
    auth, user_id = resolved
    settings = get_settings()

    try:
        with session_scope() as session:
            conn = get_active_connection(session, user_id)
            if conn is not None and required_scopes_present(conn.scopes):
                return JSONResponse(
                    {
                        "connected": True,
                        "connection_state": "connected",
                        "account_email": conn.provider_email,
                    },
                    headers={"Cache-Control": "no-store"},
                )

            state = "reauthorization_required" if conn is not None else "not_connected"
            return JSONResponse(
                {
                    "connected": False,
                    "connection_state": state,
                    "account_email": conn.provider_email if conn is not None else None,
                    "authorization_url": build_start_url(
                        settings, auth.external_user_id, product="drive"
                    ),
                },
                headers={"Cache-Control": "no-store"},
            )
    except SQLAlchemyError:
        return JSONResponse(
            {"error": "internal_error", "detail": "Backing store unavailable."},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )


@router.get("/files")
def files(
    request: Request,
    q: str = Query("", max_length=200),
    parent_id: str | None = Query(None, max_length=200),
    page_token: str | None = Query(None, max_length=4096),
    page_size: int = Query(25, ge=1, le=100),
):
    """Search accessible Drive filenames, including shared items and Shared Drives.

    Pass `parent_id` to narrow results to one folder's direct children
    instead of the whole accessible corpus.
    """
    resolved = _authenticate(request)
    if isinstance(resolved, JSONResponse):
        return resolved
    auth, user_id = resolved
    request_id = uuid.uuid4().hex
    summary = (
        f"query={'<set>' if q.strip() else '<empty>'} "
        f"parent_id={parent_id or '<none>'}"
    )

    try:
        with session_scope() as session:
            conn = get_active_connection(session, user_id)
            if conn is None:
                raise DriveProviderError(
                    "reauth_required",
                    "Connect Google Drive before searching files.",
                    status_code=401,
                )
            service = build_scoped_drive_service(session, conn, get_settings())
            result = search_files(
                service,
                query=q,
                page_token=page_token,
                page_size=page_size,
                parent_id=parent_id,
            )
    except DriveProviderError as exc:
        _audit(
            user_id=user_id,
            tool_name="google_drive_api_search_files",
            request_id=request_id,
            input_summary=summary,
            result_status="error",
            error_code=exc.code,
            auth_source=auth.source,
        )
        return _error_response(exc, external_user_id=auth.external_user_id)
    except SQLAlchemyError:
        exc = DriveProviderError(
            "internal_error", "Backing store unavailable.", status_code=503
        )
        _audit(
            user_id=user_id,
            tool_name="google_drive_api_search_files",
            request_id=request_id,
            input_summary=summary,
            result_status="error",
            error_code=exc.code,
            auth_source=auth.source,
        )
        return _error_response(exc)

    _audit(
        user_id=user_id,
        tool_name="google_drive_api_search_files",
        request_id=request_id,
        input_summary=summary,
        result_status="ok",
        error_code=None,
        auth_source=auth.source,
    )
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/files/{file_id}/content")
def content(request: Request, file_id: str):
    """Prepare and stream one bounded, shortcut-resolved Drive attachment."""
    resolved = _authenticate(request)
    if isinstance(resolved, JSONResponse):
        return resolved
    auth, user_id = resolved
    request_id = uuid.uuid4().hex
    summary = f"file_id={file_id!r}"

    try:
        with session_scope() as session:
            conn = get_active_connection(session, user_id)
            if conn is None:
                raise DriveProviderError(
                    "reauth_required",
                    "Connect Google Drive before downloading files.",
                    status_code=401,
                )
            service = build_scoped_drive_service(session, conn, get_settings())
            prepared = prepare_download(service, file_id)
    except DriveProviderError as exc:
        _audit(
            user_id=user_id,
            tool_name="google_drive_api_download_file",
            request_id=request_id,
            input_summary=summary,
            result_status="error",
            error_code=exc.code,
            auth_source=auth.source,
        )
        return _error_response(exc, external_user_id=auth.external_user_id)
    except SQLAlchemyError:
        exc = DriveProviderError(
            "internal_error", "Backing store unavailable.", status_code=503
        )
        _audit(
            user_id=user_id,
            tool_name="google_drive_api_download_file",
            request_id=request_id,
            input_summary=summary,
            result_status="error",
            error_code=exc.code,
            auth_source=auth.source,
        )
        return _error_response(exc)

    _audit(
        user_id=user_id,
        tool_name="google_drive_api_download_file",
        request_id=request_id,
        input_summary=summary,
        result_status="ok",
        error_code=None,
        auth_source=auth.source,
    )

    ascii_fallback = prepared.filename.encode("ascii", "ignore").decode() or "attachment"
    ascii_fallback = ascii_fallback.replace('"', "'").replace("\\", "_")
    disposition = (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(prepared.filename, safe='')}"
    )
    return StreamingResponse(
        _file_chunks(prepared.file),
        media_type=prepared.mime_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": disposition,
            "Content-Length": str(prepared.size),
            "X-Content-Type-Options": "nosniff",
        },
    )


def _file_chunks(file: object) -> Iterator[bytes]:
    try:
        while chunk := file.read(1024 * 1024):
            yield chunk
    finally:
        file.close()
