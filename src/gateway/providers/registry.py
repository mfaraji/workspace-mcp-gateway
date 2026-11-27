"""Tool registry and the uniform per-call pipeline.

Every tool, regardless of provider, is registered through :class:`ToolRegistry`,
which wraps it in one pipeline:

    resolve identity -> validate input -> policy/confirm check
        -> execute handler (in a worker thread) -> queue audit document (Capella)

This is the single chokepoint for identity enforcement, confirmation and audit
logging, so new providers inherit them for free.
"""

from __future__ import annotations

import inspect
import json
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as _McpToolError
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from gateway.audit.log import AuditRecord, record_audit, summarize_input
from gateway.config import Settings
from gateway.connectors.errors import ReauthRequired
from gateway.db.engine import session_scope
from gateway.identity.models import IdentityError
from gateway.identity.resolver import get_or_create_user
from gateway.mcp.context import require_current_user
from gateway.policy import confirm
from gateway.providers.base import CallContext, RiskLevel, RiskTier, ToolSpec

_CONFIRM_PARAM = "confirmation_token"


class ToolError(_McpToolError):
    """A tool failure surfaced to the MCP client, carrying a stable error code.

    Subclasses the SDK's ``ToolError`` so mcp 2.x reports its message to the model; any other
    exception is treated as a crash and reduced to "Error executing tool <name>".
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


_SYSTEM_TOOL = "system_get_current_time"


def _publish_schema(mcp: MCPServer, exposed: str, schema: dict[str, Any]) -> None:
    """Replace the schema the MCP server advertises for one registered tool.

    MCPServer has no public hook for a hand-built schema, so this touches its tool manager.
    ``pyproject.toml`` caps the ``mcp`` version this was written against; fail loudly if the
    internals move.
    """
    try:
        mcp._tool_manager.get_tool(exposed).parameters = schema
    except AttributeError as exc:  # pragma: no cover - only on an incompatible mcp release
        raise RuntimeError(
            "this mcp release no longer exposes the tool manager used to publish schemas"
        ) from exc


def _validation_message(exc: Exception) -> str:
    """One short line per problem; never the submitted values or library internals."""
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors(include_input=False, include_url=False, include_context=False):
            msg = str(err.get("msg", "invalid value")).removeprefix("Value error, ")
            loc = ".".join(str(p) for p in err.get("loc", ()))
            parts.append(f"{loc}: {msg}" if loc else msg)
        if parts:
            return "; ".join(parts)
    return "the arguments do not match the tool's input schema"


class ToolRegistry:
    """Collects ``ToolSpec``s and registers them onto an MCPServer."""

    def __init__(self) -> None:
        self._specs: list[ToolSpec] = []

    def add(self, spec: ToolSpec) -> None:
        self._specs.append(spec)

    def register_all(
        self,
        mcp: MCPServer,
        settings: Settings,
        predicate: Callable[[ToolSpec], bool] | None = None,
        names: Mapping[str, str] | None = None,
    ) -> None:
        """Register tools on ``mcp``; ``names`` maps internal -> exposed (bare) tool names.

        With ``names`` only the mapped tools (plus the system tool) are exposed, under
        their exposed names and without widget metadata. Calls still run the spec's
        pipeline, so grants, tiers and audit use the internal name.
        """
        if names is not None:
            registered = {spec.name for spec in self._specs}
            unknown = sorted(set(names) - registered)
            if unknown:
                raise ValueError(f"tool name aliases for unregistered tools: {', '.join(unknown)}")
            has_system = any(spec.name == _SYSTEM_TOOL for spec in self._specs)
            exposed_names = [*names.values(), *([_SYSTEM_TOOL] if has_system else [])]
            duplicated = sorted({n for n in exposed_names if exposed_names.count(n) > 1})
            if duplicated:
                raise ValueError(f"duplicate exposed tool names: {', '.join(duplicated)}")
        present = {
            spec.name
            for spec in self._specs
            if (predicate is None or predicate(spec))
            and (names is None or spec.name in names or spec.name == _SYSTEM_TOOL)
        }
        for spec in self._specs:
            if predicate is not None and not predicate(spec):
                continue
            exposed = spec.name
            widgets = settings.mcp_apps_enabled
            if names is not None and spec.name != _SYSTEM_TOOL:
                if spec.name not in names:
                    continue
                exposed = names[spec.name]
                widgets = False
            description = spec.description if widgets else _plain(spec.description)
            hints = [text for other, text in spec.related_tools if other in present]
            if hints:
                description = " ".join([description, *hints])
            schema = published_schema(spec)
            if names is not None:
                # Model-visible text must name tools by what this mount calls them.
                description = resolve_names(description, names)
                schema = resolve_names(schema, names)
            mcp.add_tool(
                self._build_callable(spec, settings, exposed, names),
                name=exposed,
                description=description,
                meta=spec.tool_meta if widgets else None,
            )
            # Advertise the provider model's own schema (field descriptions, limits,
            # defaults). Runtime validation stays in the pipeline, which reports
            # ``invalid_input`` and writes the audit record; MCPServer only needs the
            # permissive signature below to pass the arguments through.
            _publish_schema(mcp, exposed, schema)

    def _build_callable(
        self,
        spec: ToolSpec,
        settings: Settings,
        exposed: str | None = None,
        names: Mapping[str, str] | None = None,
    ):
        """Create an MCP-facing async function with a flat, model-derived schema."""

        async def tool(**kwargs: Any) -> Any:
            confirmation_token = kwargs.pop(_CONFIRM_PARAM, None)
            try:
                result = await anyio.to_thread.run_sync(
                    lambda: _run_pipeline(spec, settings, kwargs, confirmation_token, exposed)
                )
            except ToolError as exc:
                if not names:
                    raise
                # Runtime text is shown to the model too: use this mount's tool names.
                raise ToolError(exc.error_code, resolve_names(str(exc), names)) from exc
            if settings.mcp_apps_enabled and spec.ui_resource and isinstance(result, dict | list):
                return _with_structured_content(result)
            return result

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

        # Every mutating tool accepts the token: a user may raise any tool to MEDIUM.
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


_CONFIRM_PARAM_DESC = (
    "Leave unset on the first call. When the result is confirmation_required, ask the user "
    "and, only after an explicit yes, repeat the call with that result's confirmation_token."
)


def published_schema(spec: ToolSpec) -> dict[str, Any]:
    """The JSON schema ``tools/list`` advertises for ``spec``.

    Taken from the provider's input model so descriptions, limits and defaults reach the
    agent, keeping the inputs flat. Mutating tools also advertise ``confirmation_token``.
    """
    schema = spec.input_model.model_json_schema(by_alias=False)
    schema.pop("title", None)
    schema.pop("description", None)
    if spec.risk is RiskLevel.MUTATING:
        schema.setdefault("properties", {})[_CONFIRM_PARAM] = {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "default": None,
            "description": _CONFIRM_PARAM_DESC,
            "title": "Confirmation Token",
        }
    return schema


def resolve_names(value: Any, names: Mapping[str, str]) -> Any:
    """Rewrite internal tool names in model-visible text to a mount's exposed names.

    Walks strings, lists and dicts (a description or a JSON schema). Whole-name matches
    only, so ``google_drive_create_file`` never rewrites inside a longer identifier.
    """
    if isinstance(value, str):
        return _NAME_RE.sub(lambda m: names.get(m.group(0), m.group(0)), value)
    if isinstance(value, list):
        return [resolve_names(v, names) for v in value]
    if isinstance(value, dict):
        return {k: resolve_names(v, names) for k, v in value.items()}
    return value


_NAME_RE = re.compile(r"(?<![A-Za-z0-9_])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![A-Za-z0-9_])")

_WIDGET_NOTE = "Shows an interactive"


def _plain(description: str) -> str:
    """Drop the trailing widget note ("do not repeat the results...") when widgets are off."""
    cut = description.find(_WIDGET_NOTE)
    return description if cut < 0 else description[:cut].rstrip()


def _with_structured_content(result: dict | list) -> CallToolResult:
    """Return a widget tool's result as text (for the model) plus structuredContent (for the UI).

    structuredContent must be an object, so a bare list is wrapped as ``{"result": [...]}``.
    """
    if isinstance(result, list):
        result = {"result": result}
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=json.loads(text),
    )


def _run_pipeline(
    spec: ToolSpec,
    settings: Settings,
    raw_args: dict,
    confirmation_token: str | None,
    exposed_name: str | None = None,
) -> Any:
    """The synchronous per-call pipeline (runs in a worker thread).

    Resolves the caller, runs the tool inside one transaction, and queues exactly
    one audit document per call (after the transaction's outcome is known).
    ``exposed_name`` is what this mount calls the tool; it only appears in text shown
    to the model (grants, tiers and audit keep ``spec.name``).
    """
    try:
        auth = require_current_user()
    except IdentityError as exc:
        raise ToolError("unauthorized", str(exc)) from exc

    request_id = uuid.uuid4().hex
    input_summary = summarize_input(spec.name, raw_args)

    try:
        with session_scope() as session:
            user_id = get_or_create_user(session, auth).id
    except SQLAlchemyError as exc:
        raise ToolError("internal_error", "backing store unavailable") from exc

    started = time.monotonic()
    meta: dict[str, Any] = {"tier": None}

    def audit(status: str, error_code: str | None = None) -> None:
        tier = meta["tier"]
        record_audit(
            AuditRecord(
                user_id=user_id,
                provider=spec.provider,
                tool_name=spec.name,
                result_status=status,
                request_id=request_id,
                input_summary=input_summary,
                error_code=error_code,
                auth_source=auth.source,
                risk_tier=str(tier) if tier else None,
                duration_ms=int((time.monotonic() - started) * 1000),
                oauth_client_id=auth.oauth_client_id,
                chat_id=auth.chat_id,
            )
        )

    try:
        result = _execute(
            spec,
            settings,
            raw_args,
            confirmation_token,
            auth,
            user_id,
            request_id,
            meta,
            exposed_name,
        )
    except ToolError as exc:
        audit("error", exc.error_code)
        connect_result = _connect_required_result(
            spec, settings, auth.external_user_id, exc, exposed_name
        )
        if connect_result is not None:
            return connect_result
        raise
    except SQLAlchemyError as exc:
        # Never surface the connection string / DB internals to the client.
        audit("error", "internal_error")
        raise ToolError("internal_error", "backing store unavailable") from exc

    # The transaction has committed; record the outcome.
    audit("needs_confirmation" if meta.get("needs_confirmation") else "ok")
    return result


_CONFIRM_INSTRUCTIONS = (
    "This action needs the user's approval. Ask the user the question above in chat and wait "
    "for an explicit yes. Only after they agree, call this tool again with identical arguments "
    "plus this confirmation_token. Never confirm on the user's behalf, and drop the action if "
    "they decline."
)


def _execute(
    spec: ToolSpec,
    settings: Settings,
    raw_args: dict,
    confirmation_token: str | None,
    auth,
    user_id,
    request_id: str,
    meta: dict[str, Any],
    exposed_name: str | None = None,
) -> Any:
    """Validate, gate, and execute the tool inside a single transaction.

    Every failure path raises ``ToolError`` (carrying a stable ``error_code``); the
    caller records the audit for every outcome. ``meta["tier"]`` is filled with the
    user's effective risk tier once known.
    """
    with session_scope() as session:
        from gateway.access.policy import effective_tier, enforce

        enforce(session, user_id, settings, auth, spec)
        tier: RiskTier | None = effective_tier(session, user_id, spec)
        meta["tier"] = tier
        ctx = CallContext(
            user_id=user_id,
            external_user_id=auth.external_user_id,
            request_id=request_id,
            chat_id=auth.chat_id,
            message_id=auth.message_id,
            automation_id=auth.automation_id,
            clerk_subject=auth.clerk_subject,
            oauth_client_id=auth.oauth_client_id,
        )

        # Validate input against the declared model.
        try:
            model = spec.input_model(**raw_args)
        except Exception as exc:  # pydantic ValidationError et al.
            raise ToolError("invalid_input", f"invalid input: {_validation_message(exc)}") from exc
        account = getattr(model, "account", None)
        if account:
            ctx = replace(ctx, account=account)

        # Policy / confirmation gate. The preview is built from the validated
        # model so mutating tools can render a meaningful confirmation message.
        preview_builder = (lambda _: spec.preview_builder(model)) if spec.preview_builder else None
        decision = confirm.check(
            settings,
            spec,
            raw_args,
            ctx,
            confirmation_token=confirmation_token,
            preview_builder=preview_builder,
            tier=tier,
            display_name=exposed_name,
        )
        if isinstance(decision, confirm.NeedsConfirmation):
            meta["needs_confirmation"] = True
            return {
                "status": "confirmation_required",
                "question": f"{decision.preview}\n\nDo you want me to proceed?",
                "preview": decision.preview,
                "instructions": _CONFIRM_INSTRUCTIONS,
                "confirmation_token": decision.confirmation_token,
            }

        # Execute.
        try:
            return spec.handler(model, ctx, session)
        except ReauthRequired as exc:
            raise ToolError("reauth_required", str(exc)) from exc
        except ToolError:
            raise
        except Exception as exc:
            code = _classify_error(spec, exc)
            raise ToolError(code, f"{exposed_name or spec.name} failed") from exc


def _connect_required_result(
    spec: ToolSpec,
    settings: Settings,
    external_user_id: str,
    exc: ToolError,
    exposed_name: str | None = None,
) -> str | None:
    """Return user-facing auth guidance for recoverable provider-connection failures."""
    if exc.error_code not in {"not_connected", "reauth_required"}:
        return None

    authorization_url = _authorization_url_for_tool(spec, settings, external_user_id)
    if authorization_url is None:
        return None

    product_name = _product_name_for_tool(spec)
    return (
        f"Authorization required for {product_name}.\n\n"
        "Open this authorization URL, approve access, then retry the same request:\n"
        f"{authorization_url}\n\n"
        f"Status: {exc.error_code}\n"
        f"Tool: {exposed_name or spec.name}\n"
        "Do not create a Calendar event as a fallback unless the user explicitly asks."
    )


def _authorization_url_for_tool(
    spec: ToolSpec, settings: Settings, external_user_id: str
) -> str | None:
    """Build the connector-scoped authorization URL for a provider tool, if any."""
    from gateway.connectors import registry as connectors

    connector = connectors.by_spec(spec)
    if connector is None:
        return None
    return connector.connect_url(settings, external_user_id)


def _product_name_for_tool(spec: ToolSpec) -> str:
    from gateway.connectors import registry as connectors

    connector = connectors.by_spec(spec)
    return connector.display_name if connector is not None else spec.provider


def _classify_error(spec: ToolSpec, exc: Exception) -> str:
    """Map a provider exception to a stable, non-sensitive error code."""
    from gateway.connectors import registry as connectors

    connector = connectors.by_spec(spec)
    if connector is not None:
        code = connector.classify_error(exc)
        if code is not None:
            return code
    return "internal_error"
