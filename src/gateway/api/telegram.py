"""Private Telegram connector API for the on-host Open WebUI backend."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from gateway.config import get_settings
from gateway.db.engine import session_scope
from gateway.identity.models import AuthenticatedUser, IdentityError
from gateway.identity.resolver import get_or_create_user, resolve_identity
from gateway.providers.base import CallContext
from gateway.providers.registry import ToolError
from gateway.providers.telegram.service import (
    connect_bot,
    disconnect_bot,
    get_connection,
    list_destinations,
    remove_destination,
    verify_destination,
)

router = APIRouter(prefix="/api/telegram", tags=["telegram-integration"])


class ConnectBody(BaseModel):
    bot_token: str = Field(..., min_length=10, max_length=256)


class DestinationBody(BaseModel):
    alias: str = Field(..., min_length=2, max_length=64)
    target: str = Field(..., min_length=2, max_length=255)


def _authenticate(request: Request) -> tuple[AuthenticatedUser, uuid.UUID] | JSONResponse:
    try:
        auth = resolve_identity(dict(request.headers), get_settings())
    except IdentityError as exc:
        return _error("unauthorized", str(exc), 401)
    try:
        with session_scope() as session:
            user_id = get_or_create_user(session, auth).id
    except SQLAlchemyError:
        return _error("internal_error", "Backing store unavailable.", 503)
    return auth, user_id


def _error(code: str, detail: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"error": code, "detail": detail},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def _tool_error(exc: ToolError) -> JSONResponse:
    status = {
        "invalid_input": 400,
        "unauthorized": 401,
        "not_connected": 409,
        "forbidden": 403,
        "not_found": 404,
        "rate_limited": 429,
        "provider_timeout": 504,
    }.get(exc.error_code, 502)
    return _error(exc.error_code, str(exc), status)


def _destination_json(item) -> dict:
    return {
        "alias": item.alias,
        "chat_id": item.chat_id,
        "title": item.title,
        "username": item.username,
        "enabled": item.enabled,
        "verified_at": item.verified_at.isoformat(),
    }


@router.get("/status")
def status(request: Request):
    resolved = _authenticate(request)
    if isinstance(resolved, JSONResponse):
        return resolved
    _, user_id = resolved
    try:
        with session_scope() as session:
            conn = get_connection(session, user_id)
            if conn is None:
                return JSONResponse(
                    {"connected": False, "bot": None, "destinations": []},
                    headers={"Cache-Control": "no-store"},
                )
            return JSONResponse(
                {
                    "connected": True,
                    "bot": conn.connection_metadata,
                    "destinations": [
                        _destination_json(item) for item in list_destinations(session, user_id)
                    ],
                },
                headers={"Cache-Control": "no-store"},
            )
    except SQLAlchemyError:
        return _error("internal_error", "Backing store unavailable.", 503)


@router.put("/connection")
def put_connection(body: ConnectBody, request: Request):
    resolved = _authenticate(request)
    if isinstance(resolved, JSONResponse):
        return resolved
    _, user_id = resolved
    try:
        with session_scope() as session:
            bot = connect_bot(session, user_id, body.bot_token, get_settings())
        return JSONResponse({"connected": True, "bot": bot}, headers={"Cache-Control": "no-store"})
    except ToolError as exc:
        return _tool_error(exc)
    except SQLAlchemyError:
        return _error("internal_error", "Backing store unavailable.", 503)


@router.delete("/connection")
def delete_connection(request: Request):
    resolved = _authenticate(request)
    if isinstance(resolved, JSONResponse):
        return resolved
    _, user_id = resolved
    try:
        with session_scope() as session:
            disconnect_bot(session, user_id)
        return JSONResponse({"connected": False}, headers={"Cache-Control": "no-store"})
    except SQLAlchemyError:
        return _error("internal_error", "Backing store unavailable.", 503)


@router.post("/destinations")
def add_destination(body: DestinationBody, request: Request):
    resolved = _authenticate(request)
    if isinstance(resolved, JSONResponse):
        return resolved
    auth, user_id = resolved
    ctx = CallContext(
        user_id=user_id,
        external_user_id=auth.external_user_id,
        request_id=uuid.uuid4().hex,
    )
    try:
        with session_scope() as session:
            item = verify_destination(session, ctx, body.alias, body.target, get_settings())
            payload = _destination_json(item)
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})
    except IntegrityError:
        return _error("conflict", "That channel is already registered under another alias.", 409)
    except ToolError as exc:
        return _tool_error(exc)
    except SQLAlchemyError:
        return _error("internal_error", "Backing store unavailable.", 503)


@router.post("/destinations/{alias}/verify")
def reverify_destination(alias: str, request: Request):
    resolved = _authenticate(request)
    if isinstance(resolved, JSONResponse):
        return resolved
    auth, user_id = resolved
    try:
        with session_scope() as session:
            items = {item.alias: item for item in list_destinations(session, user_id)}
            existing = items.get(alias.lower())
            if existing is None:
                return _error("not_found", "Telegram destination not found.", 404)
            ctx = CallContext(
                user_id=user_id,
                external_user_id=auth.external_user_id,
                request_id=uuid.uuid4().hex,
            )
            item = verify_destination(
                session, ctx, existing.alias, existing.chat_id, get_settings()
            )
            payload = _destination_json(item)
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})
    except ToolError as exc:
        return _tool_error(exc)
    except SQLAlchemyError:
        return _error("internal_error", "Backing store unavailable.", 503)


@router.delete("/destinations/{alias}")
def delete_destination(alias: str, request: Request):
    resolved = _authenticate(request)
    if isinstance(resolved, JSONResponse):
        return resolved
    _, user_id = resolved
    try:
        with session_scope() as session:
            removed = remove_destination(session, user_id, alias)
        if not removed:
            return _error("not_found", "Telegram destination not found.", 404)
        return JSONResponse({"removed": True}, headers={"Cache-Control": "no-store"})
    except SQLAlchemyError:
        return _error("internal_error", "Backing store unavailable.", 503)
