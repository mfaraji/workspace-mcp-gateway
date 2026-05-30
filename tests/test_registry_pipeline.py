"""DB-backed tests for the registry pipeline's audit guarantee.

The pipeline must write exactly one ``tool_audit_log`` row per call, including
for failures. Because the tool's own transaction rolls back on error, the
error-path audit is committed in a separate transaction (see
``gateway.providers.registry._audit_error``). These tests exercise that against
a real database, since rollback semantics can't be observed without one.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import BaseModel, Field
from sqlalchemy import select

from gateway.config import get_settings
from gateway.db.engine import check_database, session_scope
from gateway.db.models import ToolAuditLog, User
from gateway.identity.models import AuthenticatedUser
from gateway.mcp import context as mcp_context
from gateway.providers.base import CallContext, RiskLevel, ToolSpec
from gateway.providers.registry import ToolError, _run_pipeline

pytestmark = pytest.mark.skipif(
    not check_database(), reason="database not reachable; run migrations first"
)


class _In(BaseModel):
    value: str = Field(...)


def _spec(name: str, handler) -> ToolSpec:
    return ToolSpec(
        name=name,
        provider="test",
        risk=RiskLevel.READ,
        description="test tool",
        input_model=_In,
        handler=handler,
    )


def _audit_rows(tool_name: str) -> list[ToolAuditLog]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(ToolAuditLog).where(ToolAuditLog.tool_name == tool_name)
            )
        )


def _run_as(external_user_id: str, spec: ToolSpec, args: dict):
    """Drive the pipeline with a current user set in the request context var."""
    token = mcp_context._current_user.set(AuthenticatedUser(external_user_id))
    try:
        return _run_pipeline(spec, get_settings(), args, None)
    finally:
        mcp_context._current_user.reset(token)


def test_failed_call_persists_exactly_one_error_audit_row():
    tool_name = f"test_fail_{uuid.uuid4().hex}"

    def handler(_model, _ctx: CallContext, _session):
        raise ToolError("not_connected", "no active connection")

    with pytest.raises(ToolError) as exc_info:
        _run_as("itest-audit-fail", _spec(tool_name, handler), {"value": "x"})
    assert exc_info.value.error_code == "not_connected"

    rows = _audit_rows(tool_name)
    assert len(rows) == 1
    row = rows[0]
    assert row.result_status == "error"
    assert row.error_code == "not_connected"
    # The FK to users.id must be valid: the user was committed up front.
    assert row.user_id is not None
    with session_scope() as session:
        assert session.get(User, row.user_id) is not None


def test_invalid_input_persists_one_error_audit_row():
    tool_name = f"test_badinput_{uuid.uuid4().hex}"

    def handler(_model, _ctx, _session):  # should never run
        raise AssertionError("handler must not run on invalid input")

    with pytest.raises(ToolError) as exc_info:
        _run_as("itest-audit-badinput", _spec(tool_name, handler), {})  # missing 'value'
    assert exc_info.value.error_code == "invalid_input"

    rows = _audit_rows(tool_name)
    assert len(rows) == 1
    assert rows[0].error_code == "invalid_input"


def test_successful_call_persists_one_ok_audit_row():
    tool_name = f"test_ok_{uuid.uuid4().hex}"

    def handler(model, _ctx, _session):
        return {"echo": model.value}

    result = _run_as("itest-audit-ok", _spec(tool_name, handler), {"value": "hello"})
    assert result == {"echo": "hello"}

    rows = _audit_rows(tool_name)
    assert len(rows) == 1
    assert rows[0].result_status == "ok"
    assert rows[0].error_code is None
