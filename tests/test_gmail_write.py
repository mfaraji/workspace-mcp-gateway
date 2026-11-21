"""Tests for Gmail draft tools: HTML drafts, reply scoping, ownership, never sending."""

from __future__ import annotations

import base64
import email
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from gateway.audit.log import summarize_input
from gateway.db.base import Base
from gateway.db.models import GmailAgentDraft, User
from gateway.providers.base import CallContext, RiskLevel
from gateway.providers.google.gmail import read, write
from gateway.providers.registry import ToolError, ToolRegistry
from tests.test_gmail_read import LABEL, LABELS, _Exec, _msg, _Resource


class _Drafts:
    def __init__(self):
        self.created = []
        self.deleted = []

    def create(self, **kw):
        self.created.append(kw)
        thread = kw["body"]["message"].get("threadId", "t9")
        return _Exec({"id": "d1", "message": {"id": "m9", "threadId": thread}})

    def delete(self, **kw):
        self.deleted.append(kw)
        return _Exec({})

    def get(self, **kw):
        return _Exec({"id": kw["id"], "message": _msg("m9", [])})

    def update(self, **kw):
        self.updated = kw
        return _Exec({"id": kw["id"]})

    def send(self, **kw):  # pragma: no cover
        raise AssertionError("send must never be called")


class _Svc:
    def __init__(self, messages):
        self.drafts_res = _Drafts()
        self.labels_res = _Resource({"list": lambda **k: {"labels": LABELS}})
        self.messages_res = _Resource({"get": lambda **k: messages[k["id"]]})

    def users(self):
        return self

    def drafts(self):
        return self.drafts_res

    def labels(self):
        return self.labels_res

    def messages(self):
        return self.messages_res


@pytest.fixture
def ctx():
    return CallContext(user_id=uuid.uuid4(), external_user_id="u", request_id="r")


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[User.__table__, GmailAgentDraft.__table__])
    with Session(engine) as s:
        yield s


def _use(monkeypatch, svc):
    conn = SimpleNamespace(provider_account_id="acct", provider_email="me@example.com")
    conn.status = "active"
    monkeypatch.setattr(write, "gmail_service_and_connection", lambda _s, _c: (svc, conn))
    monkeypatch.setattr(write, "list_connections", lambda *_a: [conn])


def _raw_message(svc):
    raw = svc.drafts_res.created[0]["body"]["message"]["raw"]
    return email.message_from_bytes(base64.urlsafe_b64decode(raw))


def test_tools_are_unconfirmed_and_no_send_tool():
    registry = ToolRegistry()
    write.register(registry)
    read.register(registry)
    names = {s.name for s in registry._specs}
    assert not any("send" in n for n in names)
    for s in registry._specs:
        if s.risk is RiskLevel.MUTATING:
            assert s.requires_confirmation is False


def test_html_draft_is_multipart_with_text_fallback(monkeypatch, ctx, session):
    svc = _Svc({})
    _use(monkeypatch, svc)
    out = write.create_draft(
        write.CreateDraftInput(to=["a@example.com"], subject="S", body_html="<p>Hello</p>"),
        ctx,
        session,
    )
    assert out["status"] == "draft_created"
    msg = _raw_message(svc)
    assert msg.get_content_type() == "multipart/alternative"
    types = [p.get_content_type() for p in msg.get_payload()]
    assert types == ["text/plain", "text/html"]
    assert "Hello" in msg.get_payload()[0].get_payload(decode=True).decode()


def test_reply_to_unlabelled_rejected(monkeypatch, ctx, session):
    _use(monkeypatch, _Svc({"m": _msg("m", ["INBOX"])}))
    with pytest.raises(ToolError) as e:
        args = write.CreateDraftInput(body_text="x", reply_to_message_id="m")
        write.create_draft(args, ctx, session)
    assert e.value.error_code == "not_found"


def test_reply_threads_message(monkeypatch, ctx, session):
    orig = _msg("m", [LABEL])
    orig["payload"]["headers"].append({"name": "Message-ID", "value": "<abc@x>"})
    svc = _Svc({"m": orig})
    _use(monkeypatch, svc)
    write.create_draft(write.CreateDraftInput(body_text="x", reply_to_message_id="m"), ctx, session)
    assert svc.drafts_res.created[0]["body"]["message"]["threadId"] == "t1"
    msg = _raw_message(svc)
    assert msg["In-Reply-To"] == "<abc@x>"
    assert msg["To"] == "a@example.com"
    assert msg["Subject"] == "Re: Hi"


def test_requires_a_body(monkeypatch, ctx, session):
    _use(monkeypatch, _Svc({}))
    with pytest.raises(ToolError):
        write.create_draft(write.CreateDraftInput(to=["a@example.com"]), ctx, session)


def test_invalid_address_rejected():
    with pytest.raises(ValueError):
        write.CreateDraftInput(to=["bad\nBcc: x@y.com"], body_text="x")


def test_update_delete_require_ownership(monkeypatch, ctx, session):
    svc = _Svc({})
    _use(monkeypatch, svc)
    with pytest.raises(ToolError) as e:
        write.delete_draft(write.DeleteDraftInput(draft_id="foreign"), ctx, session)
    assert e.value.error_code == "not_found"
    with pytest.raises(ToolError):
        write.update_draft(write.UpdateDraftInput(draft_id="foreign", subject="s"), ctx, session)
    assert svc.drafts_res.deleted == []


def test_create_then_delete_owned(monkeypatch, ctx, session):
    svc = _Svc({})
    _use(monkeypatch, svc)
    session.add(User(id=ctx.user_id, external_user_id="u"))
    session.flush()
    write.create_draft(write.CreateDraftInput(body_text="x"), ctx, session)
    session.flush()
    write.delete_draft(write.DeleteDraftInput(draft_id="d1"), ctx, session)
    assert svc.drafts_res.deleted[0]["id"] == "d1"


def test_draft_of_another_account_is_not_found(monkeypatch, ctx, session):
    svc = _Svc({})
    _use(monkeypatch, svc)
    session.add(User(id=ctx.user_id, external_user_id="u"))
    session.flush()
    write.create_draft(write.CreateDraftInput(body_text="x"), ctx, session)
    session.flush()
    other = CallContext(
        user_id=ctx.user_id, external_user_id="u", request_id="r", account="other@example.com"
    )
    with pytest.raises(ToolError) as e:
        write.delete_draft(write.DeleteDraftInput(draft_id="d1"), other, session)
    assert e.value.error_code == "not_found"
    assert svc.drafts_res.deleted == []


def test_audit_drops_content():
    s = summarize_input(
        "google_gmail_create_draft",
        {
            "to": ["a@b.com"],
            "subject": "secret",
            "body_html": "<p>x</p>",
            "reply_to_message_id": "m",
        },
    )
    assert "secret" not in s and "a@b.com" not in s and "reply_to_message_id='m'" in s
