"""Apex Clerk act-as: subject propagation, credential precedence, workspace tools."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from gateway.audit.log import summarize_input
from gateway.config import Settings
from gateway.connectors import apikey
from gateway.db.models import OAuthIdentityLink, User
from gateway.identity.models import AuthenticatedUser
from gateway.identity.resolver import get_or_create_user, resolve_identity
from gateway.oauth import clerk
from gateway.providers import registry as registry_mod
from gateway.providers.apex import auth, read
from gateway.providers.apex import client as apex_client
from gateway.providers.base import CallContext
from gateway.providers.registry import ToolError

GATEWAY_KEY = "pk_sys_gateway_secret"
USER_KEY = "pk_live_userkey"
ISSUER = "https://clerk.example.test"


def _settings(**overrides) -> Settings:
    data = dict(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
        base_url="https://mcp.example.test",
        google_client_id="cid",
        google_client_secret="secret",
        apex_base_url="http://apex.local",
        apex_gateway_api_key=GATEWAY_KEY,
        token_encryption_key="x" * 43 + "=",
        gateway_shared_secret="shared-secret",
        trusted_open_webui_origin="https://webui.example.test",
        session_secret="sess",
        clerk_issuer=ISSUER,
    )
    data.update(overrides)
    return Settings(**data)


def _ctx(subject: str | None = "user_abc") -> CallContext:
    return CallContext(
        user_id=uuid.uuid4(), external_user_id="u1", request_id="r1", clerk_subject=subject
    )


class _Cipher:
    def decrypt(self, value: bytes) -> str:
        return value.decode().removeprefix("encrypted:")


@pytest.fixture
def stores(monkeypatch):
    """Fake the stored-key and default-workspace lookups."""
    state = {"key": None, "workspace": None}

    def get_conn(_s, _u, _p):
        if state["key"] is None:
            return None
        return SimpleNamespace(token=SimpleNamespace(encrypted_access_token=state["key"]))

    monkeypatch.setattr(apex_client, "get_api_key_connection", get_conn)
    monkeypatch.setattr(apex_client, "get_default_workspace", lambda _s, _u, _p: state["workspace"])
    monkeypatch.setattr(apex_client, "get_cipher", lambda: _Cipher())
    return state


# --- strategy precedence ------------------------------------------------------


def test_stored_key_wins_over_act_as(stores):
    stores["key"] = f"encrypted:{USER_KEY}".encode()
    stores["workspace"] = {"workspace_id": "w1", "workspace_name": "W"}
    creds = apex_client.APEX_API_KEY.get_credentials(None, _ctx(), _settings())
    assert creds.api_key == USER_KEY
    assert creds.headers == {}


def test_act_as_headers_exactly_per_contract(stores):
    stores["workspace"] = {"workspace_id": "w1", "workspace_name": "W"}
    creds = apex_client.APEX_API_KEY.get_credentials(None, _ctx("user_abc"), _settings())
    assert creds.api_key == GATEWAY_KEY
    assert creds.headers == {"X-Act-As-Subject": "user_abc", "X-Workspace-Id": "w1"}


def test_act_as_without_workspace_omits_header_when_not_required(stores):
    creds = apex_client.APEX_API_KEY.get_credentials(
        None, _ctx("user_abc"), _settings(), with_workspace=False
    )
    assert creds.headers == {"X-Act-As-Subject": "user_abc"}


def test_workspace_not_set_names_the_tools(stores):
    with pytest.raises(ToolError) as exc:
        apex_client.APEX_API_KEY.get_credentials(None, _ctx(), _settings())
    assert exc.value.error_code == "workspace_not_set"
    assert "apex_list_workspaces" in str(exc.value)
    assert "apex_set_workspace" in str(exc.value)
    with pytest.raises(ToolError) as wexc:
        apex_client.APEX_WEALTH_API_KEY.get_credentials(None, _ctx(), _settings())
    assert wexc.value.error_code == "workspace_not_set"
    assert "apex_wealth_list_workspaces" in str(wexc.value)
    assert "apex_wealth_set_workspace" in str(wexc.value)


@pytest.mark.parametrize(
    "ctx,settings",
    [
        (_ctx(None), _settings()),  # no subject
        (_ctx("user_abc"), _settings(apex_gateway_api_key=None)),  # act-as disabled
    ],
)
def test_not_connected_mentions_both_options_and_no_secrets(stores, ctx, settings):
    with pytest.raises(ToolError) as exc:
        apex_client.APEX_API_KEY.get_credentials(None, ctx, settings)
    assert exc.value.error_code == "not_connected"
    msg = str(exc.value)
    assert "apex_set_api_key" in msg and "Clerk" in msg
    assert GATEWAY_KEY not in msg


def test_connect_url_still_none():
    assert apex_client.APEX_API_KEY.connect_url(_settings(), "u") is None


def test_credentials_repr_hides_secrets():
    creds = apex_client.ApexCredentials(GATEWAY_KEY, {"X-Act-As-Subject": "user_abc"})
    assert GATEWAY_KEY not in repr(creds) and "user_abc" not in repr(creds)


# --- apex_request -------------------------------------------------------------


def test_extra_headers_merged_after_authorization(monkeypatch):
    captured = {}

    def fake(method, url, params=None, json=None, headers=None, timeout=None):
        captured["headers"] = headers
        return SimpleNamespace(status_code=200, content=b"x", text="", json=lambda: [])

    monkeypatch.setattr(apex_client.httpx, "request", fake)
    apex_client.apex_request(
        _settings(), GATEWAY_KEY, "GET", "/api/workspaces", extra_headers={"X-Act-As-Subject": "s"}
    )
    assert captured["headers"] == {
        "Authorization": f"Bearer {GATEWAY_KEY}",
        "X-Act-As-Subject": "s",
    }


def test_tool_sends_act_as_headers_end_to_end(monkeypatch, stores):
    stores["workspace"] = {"workspace_id": "w1", "workspace_name": "W"}
    captured = {}

    def fake(method, url, params=None, json=None, headers=None, timeout=None):
        captured["headers"] = headers
        return SimpleNamespace(status_code=200, content=b"x", text="", json=lambda: [])

    monkeypatch.setattr(apex_client.httpx, "request", fake)
    monkeypatch.setattr(read, "get_settings", _settings)
    read.list_clients(read.ListClientsInput(), _ctx("user_abc"), None)
    assert captured["headers"] == {
        "Authorization": f"Bearer {GATEWAY_KEY}",
        "X-Act-As-Subject": "user_abc",
        "X-Workspace-Id": "w1",
    }


# --- workspace tools ----------------------------------------------------------

WORKSPACES = [
    {"id": "w-biz", "name": "Sync VC", "kind": "BUSINESS", "capabilities": ["INVOICING"]},
    {"id": "w-me", "name": "Personal", "kind": "PERSONAL", "capabilities": ["WEALTH"]},
]


@pytest.fixture
def apex_calls(monkeypatch, stores):
    calls = []

    def fake_request(settings, api_key, method, path, **kw):
        calls.append(dict(api_key=api_key, method=method, path=path, **kw))
        return WORKSPACES

    monkeypatch.setattr(apex_client, "apex_request", fake_request)
    monkeypatch.setattr(auth, "get_settings", _settings)
    return calls


def test_list_workspaces_works_without_workspace(apex_calls):
    result = auth.list_workspaces(auth.NoInput(), _ctx(), None)
    assert [w["id"] for w in result] == ["w-biz", "w-me"]
    assert apex_calls[0]["path"] == "/api/workspaces"
    assert apex_calls[0]["extra_headers"] == {"X-Act-As-Subject": "user_abc"}


def test_list_workspaces_with_stored_key_sends_no_act_as(apex_calls, stores):
    stores["key"] = f"encrypted:{USER_KEY}".encode()
    auth.list_workspaces(auth.NoInput(), _ctx(None), None)
    assert apex_calls[0]["api_key"] == USER_KEY
    assert apex_calls[0]["extra_headers"] is None


def test_wealth_list_filters_to_wealth_workspaces(apex_calls):
    result = auth.list_wealth_workspaces(auth.NoInput(), _ctx(), None)
    assert [w["id"] for w in result] == ["w-me"]


def test_set_workspace_rejects_unknown_id(apex_calls, monkeypatch):
    stored = []
    monkeypatch.setattr(auth, "set_default_workspace", lambda *a: stored.append(a))
    with pytest.raises(ToolError) as exc:
        auth.set_workspace(auth.SetWorkspaceInput(workspace_id="nope"), _ctx(), None)
    assert exc.value.error_code == "invalid_input"
    assert stored == []


def test_set_workspace_stores_valid_id(apex_calls, monkeypatch):
    stored = []
    monkeypatch.setattr(auth, "set_default_workspace", lambda *a: stored.append(a))
    ctx = _ctx()
    result = auth.set_workspace(auth.SetWorkspaceInput(workspace_id="w-biz"), ctx, None)
    assert result == {
        "status": "workspace_set",
        "workspace_id": "w-biz",
        "name": "Sync VC",
        "kind": "BUSINESS",
    }
    assert stored == [(None, ctx.user_id, "apex", "w-biz", "Sync VC")]


def test_wealth_set_workspace_rejects_business_workspace(apex_calls, monkeypatch):
    monkeypatch.setattr(auth, "set_default_workspace", lambda *a: pytest.fail("stored"))
    with pytest.raises(ToolError):
        auth.set_wealth_workspace(auth.SetWorkspaceInput(workspace_id="w-biz"), _ctx(), None)
    stored = []
    monkeypatch.setattr(auth, "set_default_workspace", lambda *a: stored.append(a))
    auth.set_wealth_workspace(auth.SetWorkspaceInput(workspace_id="w-me"), _ctx(), None)
    assert stored[0][2] == "apex_wealth"


class _Session:
    def __init__(self):
        self.rows = []

    def scalar(self, _stmt):
        return self.rows[0] if self.rows else None

    def add(self, row):
        self.rows.append(row)

    def flush(self):
        return None


def test_default_workspace_roundtrip():
    session, uid = _Session(), uuid.uuid4()
    assert apikey.get_default_workspace(session, uid, "apex") is None
    apikey.set_default_workspace(session, uid, "apex", "w1", "One")
    apikey.set_default_workspace(session, uid, "apex", "w2", "Two")
    assert len(session.rows) == 1  # upsert, not a second row
    row = session.rows[0]
    assert (row.provider, row.provider_account_id, row.status) == ("apex", "clerk", "active")
    assert row.token is None  # never a ProviderToken
    assert row.connection_metadata == {"workspace_id": "w2", "workspace_name": "Two"}
    assert apikey.get_default_workspace(session, uid, "apex") == {
        "workspace_id": "w2",
        "workspace_name": "Two",
    }


def test_audit_records_workspace_id_only():
    assert summarize_input("apex_set_workspace", {"workspace_id": "w1"}) == "workspace_id='w1'"
    assert summarize_input("apex_wealth_set_workspace", {"workspace_id": "w1"}) == (
        "workspace_id='w1'"
    )
    assert summarize_input("apex_list_workspaces", {"api_key": GATEWAY_KEY}) == ""


# --- subject propagation --------------------------------------------------------


def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    User.__table__.create(engine)
    OAuthIdentityLink.__table__.create(engine)
    return engine


def test_oauth_unlinked_and_linked_carry_subject(monkeypatch):
    monkeypatch.setattr(clerk, "verify_access_token", lambda *_a, **_k: "user_abc")
    engine = _db()
    headers = {"Authorization": "Bearer t"}
    with Session(engine) as session:
        unlinked = resolve_identity(
            headers, _settings(), session, oauth_resource="https://mcp.example.test/mcp"
        )
        assert unlinked.external_user_id.startswith("oauth:")
        assert unlinked.clerk_subject == "user_abc"
        user = get_or_create_user(session, AuthenticatedUser(external_user_id="alice"))
        session.add(OAuthIdentityLink(user_id=user.id, issuer=ISSUER, subject="user_abc"))
        session.commit()
        linked = resolve_identity(
            headers, _settings(), session, oauth_resource="https://mcp.example.test/mcp"
        )
        assert linked.external_user_id == "alice"
        assert linked.clerk_subject == "user_abc"


def _spec(provider="apex"):
    return SimpleNamespace(provider=provider)


def test_header_user_subject_resolved_from_link():
    engine = _db()
    with Session(engine) as session:
        user = get_or_create_user(session, AuthenticatedUser(external_user_id="alice"))
        session.add(OAuthIdentityLink(user_id=user.id, issuer=ISSUER, subject="user_abc"))
        session.commit()
        auth_user = AuthenticatedUser(external_user_id="alice", source="token")
        found = registry_mod._clerk_subject(_spec(), _settings(), auth_user, user.id, session)
        assert found == "user_abc"
        other = get_or_create_user(session, AuthenticatedUser(external_user_id="bob"))
        assert (
            registry_mod._clerk_subject(_spec(), _settings(), auth_user, other.id, session) is None
        )


def test_subject_passthrough_for_future_clerk_login():
    # A front door that already knows the subject needs no lookup at all.
    auth_user = AuthenticatedUser(external_user_id="x", clerk_subject="user_zzz")
    session = SimpleNamespace(scalar=lambda *_: pytest.fail("queried"))
    assert (
        registry_mod._clerk_subject(_spec(), _settings(), auth_user, None, session) == "user_zzz"
    )


@pytest.mark.parametrize(
    "settings,spec",
    [
        (_settings(clerk_issuer=None), _spec()),
        (_settings(apex_gateway_api_key=None), _spec()),
        (_settings(), _spec("google")),
    ],
)
def test_no_lookup_when_not_needed(settings, spec):
    session = SimpleNamespace(scalar=lambda *_: pytest.fail("queried"))
    auth_user = AuthenticatedUser(external_user_id="x", source="token")
    assert registry_mod._clerk_subject(spec, settings, auth_user, uuid.uuid4(), session) is None
