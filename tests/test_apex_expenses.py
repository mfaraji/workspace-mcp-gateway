"""Tests for the Apex expense tools, staged files and the one-time upload route."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.api import apex_uploads
from gateway.audit.log import summarize_input
from gateway.config import Settings
from gateway.providers.apex import client as apex_client
from gateway.providers.apex import expenses
from gateway.providers.apex.tickets import mint_ticket, verify_ticket
from gateway.providers.base import CallContext, RiskLevel, RiskTier
from gateway.providers.registry import ToolError, ToolRegistry
from gateway.storage import staged_files


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
        base_url="http://gw.local",
        google_client_id="cid",
        google_client_secret="secret",
        apex_base_url="http://apex.local",
        token_encryption_key="x" * 43 + "=",
        gateway_shared_secret="shared-secret-value",
        trusted_open_webui_origin="https://openwebui.internal",
        session_secret="sess",
    )


def _ctx() -> CallContext:
    return CallContext(
        user_id=uuid.uuid4(),
        external_user_id="u",
        request_id="r",
        clerk_subject="user_clerk",
        oauth_client_id="client-1",
    )


@pytest.fixture
def captured(monkeypatch):
    calls: list[dict] = []

    def fake_call(settings, creds, method, path, *, params=None, json=None):
        calls.append(
            {"kind": "json", "method": method, "path": path, "params": params, "json": json}
        )
        return {"id": "exp/1"} if method == "POST" else [{"id": "c1"}]

    def fake_upload(settings, creds, path, *, filename, mime_type, data):
        calls.append({"kind": "upload", "path": path, "filename": filename, "data": data})
        return {"ok": True}

    monkeypatch.setattr(expenses, "get_settings", _settings)
    monkeypatch.setattr(expenses, "apex_call", fake_call)
    monkeypatch.setattr(expenses, "apex_upload", fake_upload)
    def fake_creds(settings, ctx, *, scopes, workspace_id, capability):
        calls.append({"kind": "creds", "workspace_id": workspace_id, "capability": capability})
        return apex_client.ApexCredentials("tok", {"X-Workspace-Id": workspace_id})

    monkeypatch.setattr(
        expenses, "apex_user_credentials", lambda *a, **k: apex_client.ApexCredentials("tok")
    )
    monkeypatch.setattr(expenses, "apex_credentials", fake_creds)
    return calls


def _api(captured):
    """Calls that reached Apex, without the credential checks."""
    return [c for c in captured if c["kind"] != "creds"]


def test_tools_registered_with_expected_tiers():
    registry = ToolRegistry()
    expenses.register(registry)
    specs = {s.name: s for s in registry._specs}
    assert set(specs) == {
        "apex_list_expense_categories",
        "apex_create_expense",
        "apex_upload_expense_receipt",
    }
    assert specs["apex_list_expense_categories"].risk is RiskLevel.READ
    assert specs["apex_create_expense"].default_tier is RiskTier.LOW
    assert specs["apex_upload_expense_receipt"].default_tier is RiskTier.LOW


def test_list_categories_passes_kind(captured):
    expenses.list_categories(expenses.ListExpenseCategoriesInput(kind="business"), _ctx(), None)
    assert captured[0]["path"] == "/api/expense-categories"
    assert captured[0]["params"] == {"kind": "business"}


def test_create_expense_builds_body_and_stable_external_ref(captured):
    ctx = _ctx()
    args = expenses.CreateExpenseInput(
        merchant="Home Depot", amount=42.5, date="2026-11-20", category_id="c1", workspace_id="w1"
    )
    expenses.create_expense(args, ctx, None)
    expenses.create_expense(args, ctx, None)
    calls = _api(captured)
    first, second = calls[0]["json"], calls[1]["json"]
    assert calls[0]["path"] == "/api/expense-inbox"
    assert first["merchant"] == "Home Depot" and first["categoryId"] == "c1"
    assert first["workspaceId"] == "w1" and first["externalRef"] == second["externalRef"]


def test_create_expense_from_receipt_alone_omits_scannable_fields(monkeypatch, captured):
    monkeypatch.setattr(
        expenses,
        "take",
        lambda *_: staged_files.StagedContent("r.pdf", "application/pdf", b"x", "s"),
    )
    expenses.create_expense(
        expenses.CreateExpenseInput(receipt_file_ref="ref", workspace_id="w1"), _ctx(), None
    )
    calls = _api(captured)
    assert set(calls[0]["json"]) == {"externalRef", "workspaceId"}
    assert calls[1]["kind"] == "upload"


def test_create_expense_without_receipt_requires_details():
    with pytest.raises(ValueError):
        expenses.CreateExpenseInput(merchant="x", workspace_id="w1")


def test_create_expense_validates_workspace_for_receipt_review(captured):
    args = expenses.CreateExpenseInput(
        merchant="HD", amount=5, date="2026-11-20", workspace_id="w1"
    )
    expenses.create_expense(args, _ctx(), None)
    assert captured[0] == {"kind": "creds", "workspace_id": "w1", "capability": "receiptReview"}
    assert _api(captured)[0]["json"]["workspaceId"] == "w1"


def test_also_workspaces_are_validated_and_sent(captured):
    args = expenses.CreateExpenseInput(
        merchant="HD",
        amount=5,
        date="2026-11-20",
        workspace_id="rent",
        also_workspace_ids=["biz", "rent", "biz"],
    )
    expenses.create_expense(args, _ctx(), None)
    creds = [c["workspace_id"] for c in captured if c["kind"] == "creds"]
    assert creds == ["rent", "biz"]
    assert _api(captured)[0]["json"]["suggestedWorkspaceIds"] == ["biz"]


def test_workspace_id_is_mandatory():
    with pytest.raises(ValueError):
        expenses.CreateExpenseInput(merchant="HD", amount=5, date="2026-11-20")
    with pytest.raises(ValueError):
        expenses.CreateExpenseInput(merchant="HD", amount=5, date="2026-11-20", workspace_id="")
    with pytest.raises(ValueError):
        expenses.UploadExpenseReceiptInput(expense_id="e1")


def test_unknown_workspace_is_rejected_before_calling_apex(monkeypatch, captured):
    def deny(*a, **k):
        raise ToolError("invalid_input", "workspace_id is not one of your Apex workspaces")

    monkeypatch.setattr(expenses, "apex_credentials", deny)
    args = expenses.CreateExpenseInput(
        merchant="HD", amount=5, date="2026-11-20", workspace_id="nope"
    )
    with pytest.raises(ToolError):
        expenses.create_expense(args, _ctx(), None)
    assert captured == []


def test_create_expense_rejects_bad_input():
    with pytest.raises(ValueError):
        expenses.CreateExpenseInput(merchant="x", amount=0, date="2026-11-20", workspace_id="w")
    with pytest.raises(ValueError):
        expenses.CreateExpenseInput(merchant="x", amount=1, date="20/11/2026", workspace_id="w")


def test_create_expense_with_file_ref_uploads_receipt(monkeypatch, captured):
    staged = staged_files.StagedContent("r.pdf", "application/pdf", b"%PDF", "sha")
    monkeypatch.setattr(expenses, "take", lambda _s, _u, ref: staged)
    args = expenses.CreateExpenseInput(
        merchant="HD", amount=5, date="2026-11-20", receipt_file_ref="ref", workspace_id="w1"
    )
    out = expenses.create_expense(args, _ctx(), None)
    assert out["receipt_uploaded"] is True
    upload = _api(captured)[1]
    assert upload["path"] == "/api/expense-inbox/exp%2F1/receipt?workspaceId=w1"
    assert upload["data"] == b"%PDF"


def test_create_expense_reports_receipt_failure_without_losing_expense(monkeypatch, captured):
    monkeypatch.setattr(
        expenses,
        "take",
        lambda *_: staged_files.StagedContent("r.pdf", "application/pdf", b"x", "s"),
    )

    def boom(*a, **k):
        raise ToolError("provider_error", "nope")

    monkeypatch.setattr(expenses, "apex_upload", boom)
    args = expenses.CreateExpenseInput(
        merchant="HD", amount=5, date="2026-11-20", receipt_file_ref="ref", workspace_id="w1"
    )
    out = expenses.create_expense(args, _ctx(), None)
    assert out["id"] == "exp/1" and out["receipt_uploaded"] is False
    assert out["receipt_error"] == "provider_error"


def test_upload_without_file_ref_returns_one_time_ticket(captured):
    out = expenses.upload_receipt(
        expenses.UploadExpenseReceiptInput(expense_id="e1", workspace_id="w1"), _ctx(), None
    )
    assert out["status"] == "upload_required"
    assert out["upload_url"] == "http://gw.local/uploads/apex-receipt"
    assert _api(captured) == []  # nothing sent to Apex yet
    assert captured[0]["workspace_id"] == "w1"  # workspace validated when the ticket is issued


def test_upload_with_file_ref_sends_workspace(monkeypatch, captured):
    staged = staged_files.StagedContent("r.pdf", "application/pdf", b"%PDF", "sha")
    monkeypatch.setattr(expenses, "take", lambda _s, _u, ref: staged)
    args = expenses.UploadExpenseReceiptInput(expense_id="e1", workspace_id="w1", file_ref="ref")
    expenses.upload_receipt(args, _ctx(), None)
    assert _api(captured)[0]["path"] == "/api/expense-inbox/e1/receipt?workspaceId=w1"


def test_ticket_roundtrip_and_tamper():
    settings = _settings()
    uid = uuid.uuid4()
    token = mint_ticket(
        settings,
        user_id=uid,
        clerk_subject="u",
        oauth_client_id="c",
        expense_id="e1",
        workspace_id="w1",
    )
    ticket = verify_ticket(settings, token)
    assert ticket and ticket.user_id == uid and ticket.expense_id == "e1"
    assert ticket.workspace_id == "w1"
    assert verify_ticket(settings, token + "x") is None
    assert verify_ticket(settings, "garbage") is None


def test_audit_summaries_drop_free_text():
    out = summarize_input(
        "apex_create_expense", {"category_id": "c1", "merchant": "Secret", "amount": 9}
    )
    assert "c1" in out and "Secret" not in out and "9" not in out
    assert "e1" in summarize_input("apex_upload_expense_receipt", {"expense_id": "e1"})


# --- staged files -------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, row=None):
        self.row, self.deleted = row, False

    def scalars(self, _stmt):
        return SimpleNamespace(first=lambda: self.row)

    def delete(self, _row):
        self.deleted = True

    def flush(self):
        pass


@pytest.fixture
def cipher(monkeypatch):
    monkeypatch.setattr(
        staged_files,
        "get_cipher",
        lambda: SimpleNamespace(encrypt=lambda s: s.encode(), decrypt=lambda b: b.decode()),
    )


def _row(user_id, expires_in=timedelta(minutes=5)):
    import base64

    return SimpleNamespace(
        user_id=user_id,
        filename="a.pdf",
        mime_type="application/pdf",
        sha256="s",
        content=base64.b64encode(b"data").decode().encode(),
        expires_at=datetime.now(UTC) + expires_in,
    )


def test_take_returns_and_deletes_for_owner(cipher):
    uid = uuid.uuid4()
    session = _FakeSession(_row(uid))
    out = staged_files.take(session, uid, str(uuid.uuid4()))
    assert out.data == b"data" and session.deleted


def test_take_rejects_foreign_expired_and_malformed(cipher):
    uid = uuid.uuid4()
    for session, ref in (
        (_FakeSession(_row(uuid.uuid4())), str(uuid.uuid4())),
        (_FakeSession(_row(uid, timedelta(minutes=-1))), str(uuid.uuid4())),
        (_FakeSession(None), str(uuid.uuid4())),
        (_FakeSession(_row(uid)), "not-a-uuid"),
    ):
        with pytest.raises(ToolError) as e:
            staged_files.take(session, uid, ref)
        assert e.value.error_code == "not_found"
        assert not session.deleted


def test_stage_rejects_oversize():
    with pytest.raises(ToolError) as e:
        staged_files.stage(
            None,
            uuid.uuid4(),
            filename="a",
            mime_type="x",
            data=b"0" * (staged_files.MAX_FILE_BYTES + 1),
            source="t",
        )
    assert e.value.error_code == "too_large"


# --- upload route -------------------------------------------------------------------------


@pytest.fixture
def route(monkeypatch):
    app = FastAPI()
    app.include_router(apex_uploads.router)
    settings = _settings()
    state = {"fresh": True, "uploads": [], "audits": []}
    monkeypatch.setattr(apex_uploads, "get_settings", lambda: settings)

    class _Scope:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(apex_uploads, "session_scope", lambda: _Scope())
    monkeypatch.setattr(apex_uploads, "consume_ticket", lambda *_: state["fresh"])
    monkeypatch.setattr(apex_uploads, "_mint", lambda *a, **k: ("tok", "jti"))
    monkeypatch.setattr(
        apex_uploads,
        "_upload_to_expense",
        lambda s, c, eid, wid, name, mime, data: state["uploads"].append((eid, name, mime, data))
        or {},
    )
    monkeypatch.setattr(apex_uploads, "record_audit", state["audits"].append)
    ticket = mint_ticket(
        settings,
        user_id=uuid.uuid4(),
        clerk_subject="u",
        oauth_client_id="c",
        expense_id="e1",
        workspace_id="w1",
    )
    return TestClient(app), ticket, state


def _post(client, ticket, mime="application/pdf", data=b"%PDF-1"):
    headers = {"Authorization": f"Bearer {ticket}"} if ticket else {}
    return client.post(
        "/uploads/apex-receipt", headers=headers, files={"file": ("r.pdf", data, mime)}
    )


def test_route_uploads_with_valid_ticket_and_audits(route):
    client, ticket, state = route
    resp = _post(client, ticket)
    assert resp.status_code == 200
    assert state["uploads"] == [("e1", "r.pdf", "application/pdf", b"%PDF-1")]
    audit = state["audits"][0]
    assert audit.tool_name == "apex_upload_expense_receipt" and audit.result_status == "ok"


def test_route_rejects_missing_bad_and_reused_tickets(route):
    client, ticket, state = route
    assert _post(client, None).status_code == 401
    assert _post(client, ticket + "x").status_code == 401
    state["fresh"] = False
    assert _post(client, ticket).status_code == 401
    assert state["uploads"] == []


def test_route_rejects_bad_type_and_oversize(route):
    client, ticket, _ = route
    assert _post(client, ticket, mime="text/html").status_code == 415
    big = b"0" * (staged_files.MAX_FILE_BYTES + 1)
    assert _post(client, ticket, data=big).status_code == 413


def test_apex_user_credentials_mints_without_workspace(monkeypatch):
    seen = {}
    monkeypatch.setattr(apex_client, "user_workspaces", lambda *a: [{"id": "w"}])
    monkeypatch.setattr(
        apex_client, "_mint", lambda s, sub, cid, scopes, wid: seen.update(wid=wid) or ("t", "j")
    )
    creds = apex_client.apex_user_credentials(_settings(), _ctx(), scopes=["apex:expenses.read"])
    assert seen["wid"] is None and creds.headers == {}
