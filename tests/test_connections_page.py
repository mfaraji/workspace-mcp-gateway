"""Connections page: auth boundary, listing, and per-connector status mapping."""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from gateway.access import connections
from gateway.connectors.registry import CONNECTORS, by_slug
from gateway.identity.models import AuthenticatedUser
from tests.test_clerk_oauth import _settings


def _request(signed_in: bool = True) -> Request:
    settings = _settings()
    session = {}
    if signed_in:
        session = {
            "identity": {
                "issuer": settings.clerk_issuer,
                "sub": "alice",
                "external": "alice",
                "client": "browser",
                "expires": time.time() + 600,
            },
            "csrf": "csrf",
        }
    catalog = [{"slug": c.slug, "tools": [{"name": "t"}]} for c in CONNECTORS]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/connections",
            "headers": [],
            "session": session,
            "app": SimpleNamespace(state=SimpleNamespace(access_catalog=catalog)),
        }
    )


AUTH = AuthenticatedUser("alice", source="oauth", clerk_subject="alice", oauth_client_id="c")


def _status(slug, settings=None, auth=AUTH):
    settings = settings or SimpleNamespace(capella_configured=False, apex_gateway_api_key=None)
    return connections.connection_status(by_slug(slug), object(), "uid", auth, settings)


def test_page_redirects_to_login_without_session():
    request = _request(signed_in=False)
    response = connections.page(request)
    assert response.status_code == 303
    assert response.headers["location"] == "/access/login"
    assert request.session["return_to"] == "/connections"


def test_api_requires_browser_session(monkeypatch):
    monkeypatch.setattr(connections, "get_settings", _settings)
    monkeypatch.setattr("gateway.access.routes.get_settings", _settings)
    with pytest.raises(HTTPException) as exc:
        connections.connections(_request(signed_in=False))
    assert exc.value.status_code == 401


def test_api_lists_every_connector_without_secrets(monkeypatch):
    @contextmanager
    def scope():
        yield object()

    monkeypatch.setattr(connections, "session_scope", scope)
    monkeypatch.setattr(connections, "get_settings", _settings)
    monkeypatch.setattr("gateway.access.routes.get_settings", _settings)
    monkeypatch.setattr(connections, "get_or_create_user", lambda _s, _a: SimpleNamespace(id="uid"))
    monkeypatch.setattr(
        connections,
        "connection_status",
        lambda *_: {"status": "not_connected", "account": None, "action": None},
    )
    monkeypatch.setattr(
        connections,
        "bundle_status",
        lambda *_: {"status": "not_connected", "account": None, "action": "oauth"},
    )
    data = connections.connections(_request())
    bundles = [c for c in data["connectors"] if c.get("bundle")]
    assert [b["slug"] for b in bundles] == ["google"]
    assert bundles[0]["oauth_product"] == "all"
    assert [c["slug"] for c in data["connectors"] if not c.get("bundle")] == [
        c.slug for c in CONNECTORS if not c.hidden
    ]
    assert data["csrf"] == "csrf"
    assert all(c["mcp_url"].endswith(f"/mcp/{c['slug']}") for c in data["connectors"])
    assert all(c["tool_count"] == 1 for c in data["connectors"] if not c.get("bundle"))
    # The Google bundle serves every Google connector's tools.
    assert bundles[0]["tool_count"] == 4
    scopes = {c["slug"]: c["scope"] for c in data["connectors"]}
    assert scopes["finance"] == "universal"
    assert {v for k, v in scopes.items() if k != "finance"} == {"user"}
    assert not any("token" in k or "key" in k for c in data["connectors"] for k in c)


def test_finance_is_available_and_recipes_depend_on_capella():
    assert _status("finance")["status"] == "available"
    assert _status("recipes")["status"] == "not_configured"
    configured = SimpleNamespace(capella_configured=True, apex_gateway_api_key=None)
    assert _status("recipes", configured)["status"] == "available"


def _google(monkeypatch, *conns):
    monkeypatch.setattr(connections, "list_connections", lambda *_: list(conns))


def _conn(scopes, email, *, default=False, status="active"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        scopes=list(scopes),
        provider_email=email,
        is_default=default,
        status=status,
        last_used_at=None,
        connection_metadata={},
    )


def test_google_status(monkeypatch):
    _google(monkeypatch)
    assert _status("calendar")["status"] == "not_connected"

    needed = by_slug("calendar").upstream.required_scopes
    _google(monkeypatch, _conn(needed, "a@example.com", default=True))
    result = _status("calendar")
    assert (result["status"], result["account"]) == ("connected", "a@example.com")

    _google(monkeypatch, _conn([], "a@example.com", default=True))
    assert _status("calendar")["status"] == "reauth_required"


def test_google_status_lists_every_account_with_default(monkeypatch):
    needed = by_slug("gmail").upstream.required_scopes
    _google(
        monkeypatch,
        _conn(needed, "a@example.com", default=True),
        _conn(needed, "b@example.com"),
    )
    result = _status("gmail")
    assert [(a["email"], a["is_default"]) for a in result["accounts"]] == [
        ("a@example.com", True),
        ("b@example.com", False),
    ]
    assert result["account"] == "a@example.com"


def test_apex_status(monkeypatch):
    workspaces = [
        {"id": "w1", "name": "Acme", "kind": "BUSINESS", "capabilities": ["invoicing"]},
        {"id": "w2", "name": "Main", "kind": "PERSONAL", "capabilities": ["investments"]},
    ]
    monkeypatch.setattr(connections, "user_workspaces", lambda *_: workspaces)
    result = _status("apex")
    assert (result["status"], result["account"], result["action"]) == (
        "connected",
        "2 workspaces",
        None,
    )
    # Wealth counts only the wealth-bearing (PERSONAL) workspaces.
    assert _status("apex-wealth")["account"] == "1 workspace"

    monkeypatch.setattr(connections, "user_workspaces", lambda *_: None)
    result = _status("apex")
    assert (result["status"], result["account"]) == ("not_connected", "No Apex account")


def test_telegram_status(monkeypatch):
    monkeypatch.setattr(connections, "get_telegram_connection", lambda *_: None)
    assert _status("telegram")["status"] == "not_connected"
    monkeypatch.setattr(
        connections,
        "get_telegram_connection",
        lambda *_: SimpleNamespace(connection_metadata={"username": "mybot"}),
    )
    result = _status("telegram")
    assert (result["status"], result["account"]) == ("connected", "@mybot")


def test_root_redirects_to_connections():
    from fastapi.testclient import TestClient

    from gateway.app import create_app

    response = TestClient(create_app(), follow_redirects=False).get("/?connected=google")
    assert response.status_code == 303
    assert response.headers["location"] == "/connections"


def test_gmail_accounts_report_sharing_with_safe_default(monkeypatch):
    needed = by_slug("gmail").upstream.required_scopes
    shared = _conn(needed, "b@example.com")
    shared.connection_metadata = {"gmail_sharing": {"mode": "all", "labels": []}}
    _google(monkeypatch, _conn(needed, "a@example.com", default=True), shared)
    accounts = _status("gmail")["accounts"]
    assert accounts[0]["sharing"] == {"mode": "labels", "labels": ["agent-access"]}
    assert accounts[1]["sharing"] == {"mode": "all", "labels": []}
    calendar = _status("calendar")
    assert all("sharing" not in a for a in calendar["accounts"])
