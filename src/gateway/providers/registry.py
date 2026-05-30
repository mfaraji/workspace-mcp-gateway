"""Tool registry and the uniform per-call pipeline.

Every tool, regardless of provider, is registered through :class:`ToolRegistry`,
which wraps it in one pipeline:

    resolve identity -> validate input -> policy/confirm check
        -> execute handler (in a worker thread) -> write audit row

This is the single chokepoint for identity enforcement and audit logging, so new
providers and the future mutating-tool confirmation flow inherit them for free.
"""

from __future__ import annotations

import inspect
import uuid
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from sqlalchemy.exc import SQLAlchemyError

from gateway.audit.log import summarize_input, write_audit
from gateway.config import Settings
from gateway.db.engine import session_scope
from gateway.identity.models import IdentityError
from gateway.identity.resolver import get_or_create_user
from gateway.mcp.context import require_current_user
from gateway.policy import confirm
from gateway.providers.base import CallContext, RiskLevel, ToolSpec
from gateway.providers.google.connections import ReauthRequired

_CONFIRM_PARAM = "confirmation_token"


class ToolError(Exception):
    """A tool failure surfaced to the MCP client, carrying a stable error code."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class ToolRegistry:
    """Collects ``ToolSpec``s and registers them onto a FastMCP server."""

    def __init__(self) -> None:
        self._specs: list[ToolSpec] = []

    def add(self, spec: ToolSpec) -> None:
        self._specs.append(spec)

    def register_all(self, mcp: FastMCP, settings: Settings) -> None:
        for spec in self._specs:
            mcp.add_tool(
                self._build_callable(spec, settings),
                name=spec.name,
                description=spec.description,
            )

    def _build_callable(self, spec: ToolSpec, settings: Settings):
        """Create an MCP-facing async function with a flat, model-derived schema."""

        async def tool(**kwargs: Any) -> Any:
            confirmation_token = kwargs.pop(_CONFIRM_PARAM, None)
            return await anyio.to_thread.run_sync(
                lambda: _run_pipeline(spec, settings, kwargs, confirmation_token)
            )

        # Synthesize a flat signature from the input model's fields so the tool
        # schema is ``{field: ...}`` rather than ``{params: {...}}``.
        params = []
        annotations: dict[str, Any] = {}
        for name, field in spec.input_model.model_fields.items():
            default = inspect.Parameter.empty if field.is_required() else field.default
            params.append(
                inspect.Parameter(
                    name,
                    inspect.Parameter.KEYWORD_ONLY,
                    default=default,
                    annotation=field.annotation,
                )
            )
            annotations[name] = field.annotation

        if spec.risk is RiskLevel.MUTATING:
            params.append(
                inspect.Parameter(
                    _CONFIRM_PARAM,
                    inspect.Parameter.KEYWORD_ONLY,
                    default=None,
                    annotation=(str | None),
                )
            )
            annotations[_CONFIRM_PARAM] = str | None

        annotations["return"] = Any
        tool.__signature__ = inspect.Signature(params)
        tool.__annotations__ = annotations
        tool.__name__ = spec.name
        return tool


def _run_pipeline(
    spec: ToolSpec, settings: Settings, raw_args: dict, confirmation_token: str | None
) -> Any:
    """The synchronous per-call pipeline (runs in a worker thread).

    Resolves the caller, runs the tool inside one transaction, and guarantees
    exactly one audit row per call. Success/needs-confirmation audits commit
    atomically with the call; failures audit in a separate transaction (since the
    tool's own transaction rolls back) — see :func:`_audit_error`.
    """
    try:
        auth = require_current_user()
    except IdentityError as exc:
        raise ToolError("unauthorized", str(exc)) from exc

    request_id = uuid.uuid4().hex
    input_summary = summarize_input(spec.name, raw_args)

    # Resolve the user in its own committed transaction so the user row is
    # durable: an error-path audit row's FK to users.id must stay valid even when
    # the tool's own transaction rolls back.
    try:
        with session_scope() as session:
            user_id = get_or_create_user(session, auth).id
    except SQLAlchemyError as exc:
        raise ToolError("internal_error", "backing store unavailable") from exc

    try:
        return _execute(
            spec, settings, raw_args, confirmation_token,
            auth, user_id, request_id, input_summary,
        )
    except ToolError as exc:
        _audit_error(spec, user_id, request_id, input_summary, exc.error_code)
        raise
    except SQLAlchemyError as exc:
        # Never surface the connection string / DB internals to the client.
        _audit_error(spec, user_id, request_id, input_summary, "internal_error")
        raise ToolError("internal_error", "backing store unavailable") from exc


def _execute(
    spec: ToolSpec,
    settings: Settings,
    raw_args: dict,
    confirmation_token: str | None,
    auth,
    user_id,
    request_id: str,
    input_summary: str,
) -> Any:
    """Validate, gate, and execute the tool inside a single transaction.

    Every failure path raises ``ToolError`` (carrying a stable ``error_code``);
    the caller records the audit row for those, so this function only writes the
    committed-path audits (``ok`` and ``needs_confirmation``).
    """
    with session_scope() as session:
        ctx = CallContext(
            user_id=user_id, external_user_id=auth.external_user_id, request_id=request_id
        )

        # Validate input against the declared model.
        try:
            model = spec.input_model(**raw_args)
        except Exception as exc:  # pydantic ValidationError et al.
            raise ToolError("invalid_input", f"invalid input: {exc}") from exc

        # Policy / confirmation gate. The preview is built from the validated
        # model so mutating tools can render a meaningful confirmation message.
        preview_builder = (
            (lambda _: spec.preview_builder(model)) if spec.preview_builder else None
        )
        decision = confirm.check(
            settings, spec, raw_args, ctx,
            confirmation_token=confirmation_token, preview_builder=preview_builder,
        )
        if isinstance(decision, confirm.NeedsConfirmation):
            write_audit(
                session, user_id=user_id, provider=spec.provider, tool_name=spec.name,
                request_id=request_id, input_summary=input_summary,
                result_status="needs_confirmation",
            )
            return {
                "status": "confirmation_required",
                "preview": decision.preview,
                "confirmation_token": decision.confirmation_token,
            }

        # Execute.
        try:
            result = spec.handler(model, ctx, session)
        except ReauthRequired as exc:
            raise ToolError("reauth_required", str(exc)) from exc
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(_classify_error(exc), f"{spec.name} failed") from exc

        write_audit(
            session, user_id=user_id, provider=spec.provider, tool_name=spec.name,
            request_id=request_id, input_summary=input_summary, result_status="ok",
        )
        return result


def _audit_error(
    spec: ToolSpec, user_id, request_id: str, input_summary: str, error_code: str
) -> None:
    """Record a failed call in its own transaction (the tool's own rolled back).

    Best effort: if the store is itself unavailable there is nothing more to do,
    and we must not mask the original error with a secondary failure.
    """
    try:
        with session_scope() as session:
            write_audit(
                session, user_id=user_id, provider=spec.provider, tool_name=spec.name,
                request_id=request_id, input_summary=input_summary,
                result_status="error", error_code=error_code,
            )
    except SQLAlchemyError:
        pass


def _classify_error(exc: Exception) -> str:
    """Map a provider exception to a stable, non-sensitive error code."""
    try:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError):
            status = getattr(exc.resp, "status", None)
            return {
                401: "unauthorized",
                403: "forbidden",
                404: "not_found",
                429: "rate_limited",
            }.get(int(status) if status else 0, "provider_error")
    except Exception:
        pass
    return "internal_error"
