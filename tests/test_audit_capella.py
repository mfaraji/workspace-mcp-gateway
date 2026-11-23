"""Tool-call audit lives in Capella: one expiring, redacted document per call."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from gateway.audit import log as audit_log
from gateway.audit.log import AuditRecord, build_document, flush_audit, record_audit
from gateway.storage.capella import client as client_mod
from tests.capella_fakes import capella_settings, fake_client


@pytest.fixture
def capella(monkeypatch):
    settings = capella_settings(audit_retention_days=30)
    client, cluster = fake_client(settings)
    client_mod.set_client(client)
    monkeypatch.setattr(audit_log, "get_settings", lambda: settings)
    yield cluster
    flush_audit()
    client_mod.set_client(None)


def _record(**overrides) -> AuditRecord:
    base = dict(
        user_id=uuid.uuid4(),
        provider="google",
        tool_name="google_calendar_delete_event",
        result_status="ok",
        request_id="r1",
        input_summary="event_id='e1'",
        auth_source="oauth",
        risk_tier="medium",
        duration_ms=12,
        oauth_client_id="client-a",
    )
    base.update(overrides)
    return AuditRecord(**base)


def test_audit_document_is_owner_scoped_and_expires_after_retention(capella):
    record = _record()
    record_audit(record)
    flush_audit()

    ((key, (doc, _cas)),) = capella.collection_.docs.items()
    assert key.startswith(f"user::{record.user_id}::audit::")
    assert doc["owner_user_id"] == str(record.user_id)
    assert doc["tool_name"] == "google_calendar_delete_event"
    assert doc["result_status"] == "ok" and doc["risk_tier"] == "medium"
    assert doc["duration_ms"] == 12 and doc["created_at"].endswith("Z")
    assert "user_id" not in doc  # the owner is stored once, as owner_user_id

    (options,) = capella.collection_.insert_options[key]
    assert options["expiry"] == timedelta(days=30)


def test_retention_follows_the_setting(monkeypatch, capella):
    monkeypatch.setattr(
        audit_log, "get_settings", lambda: capella_settings(audit_retention_days=7)
    )
    record_audit(_record())
    flush_audit()
    (options,) = next(iter(capella.collection_.insert_options.values()))
    assert options["expiry"] == timedelta(days=7)


def test_none_fields_are_omitted():
    doc = build_document(_record(error_code=None, chat_id=None), uuid.uuid4())
    assert "error_code" not in doc and "chat_id" not in doc


def test_unconfigured_capella_drops_the_record_without_error(monkeypatch):
    monkeypatch.setattr(audit_log, "get_settings", lambda: capella_settings(capella_username=None))
    called = []
    monkeypatch.setattr(audit_log, "_write", called.append)
    record_audit(_record())
    flush_audit()
    assert called == []


def test_storage_failure_never_raises_into_the_tool_call(capella):
    capella.collection_.fail_with = RuntimeError("capella is down")
    record_audit(_record())  # must not raise
    flush_audit()
    assert capella.collection_.docs == {}
