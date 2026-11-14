"""First-connect Gmail onboarding: label creation, one-time instruction, safe failure."""

from __future__ import annotations

from types import SimpleNamespace

from gateway.oauth.google import (
    GMAIL_LABEL_SCOPE,
    GMAIL_READ_SCOPE,
    GMAIL_SCOPES,
    GMAIL_TOOL_SCOPES,
)
from gateway.providers.google.gmail import onboarding
from gateway.providers.google.gmail.common import GMAIL_OAUTH

NAME = "agent-access"


class _Exec:
    def __init__(self, result=None, error=None):
        self._result, self._error = result, error

    def execute(self):
        if self._error:
            raise self._error
        return self._result


class _Labels:
    def __init__(self, existing=(), create_error=None):
        self.existing = [{"id": f"L{i}", "name": n} for i, n in enumerate(existing)]
        self.created: list[dict] = []
        self.create_error = create_error

    def list(self, **_):
        return _Exec({"labels": self.existing})

    def create(self, userId, body):  # noqa: N803
        self.created.append(body)
        return _Exec({"id": "new", **body}, error=self.create_error)


def _service(labels):
    users = SimpleNamespace(labels=lambda: labels)
    return SimpleNamespace(users=lambda: users)


def test_label_scope_is_requested_but_not_required_by_tools():
    assert GMAIL_LABEL_SCOPE in GMAIL_SCOPES
    # Existing connections predate the label scope and must keep working.
    assert GMAIL_LABEL_SCOPE not in GMAIL_TOOL_SCOPES
    assert set(GMAIL_OAUTH.required_scopes) == set(GMAIL_TOOL_SCOPES)


def test_creates_label_when_missing_case_insensitive_match_otherwise():
    labels = _Labels(existing=["Work"])
    assert onboarding.ensure_access_label(_service(labels), NAME) == "created"
    assert labels.created[0]["name"] == NAME

    labels = _Labels(existing=["Agent-Access"])
    assert onboarding.ensure_access_label(_service(labels), NAME) == "exists"
    assert labels.created == []


def test_concurrent_create_conflict_counts_as_existing():
    error = Exception("conflict")
    error.resp = SimpleNamespace(status=409)
    labels = _Labels(create_error=error)
    assert onboarding.ensure_access_label(_service(labels), NAME) == "exists"


def test_provision_never_raises_and_falls_back_to_manual():
    # Without the label scope Gmail would reject the create call; do not even try.
    labels = _Labels()
    status = onboarding.provision_label(lambda: _service(labels), [GMAIL_READ_SCOPE], NAME)
    assert (status, labels.created) == ("manual", [])

    def boom():
        raise RuntimeError("gmail down")

    assert onboarding.provision_label(boom, GMAIL_SCOPES, NAME) == "manual"
    assert onboarding.provision_label(lambda: _service(_Labels()), GMAIL_SCOPES, NAME) == "created"


def test_only_first_connect_with_gmail_access_is_onboarded():
    assert onboarding.needs_onboarding(GMAIL_SCOPES, {}) is True
    assert onboarding.needs_onboarding(GMAIL_SCOPES, None) is True
    assert onboarding.needs_onboarding(GMAIL_SCOPES, {onboarding.ONBOARDED_KEY: True}) is False
    # Calendar-only connections have nothing to onboard.
    calendar = ["https://www.googleapis.com/auth/calendar.readonly"]
    assert onboarding.needs_onboarding(calendar, {}) is False


def test_notice_names_label_and_explains_usage_for_each_status():
    for status in ("created", "exists", "manual"):
        notice = onboarding.build_notice(status, NAME)
        assert notice["status"] == status and notice["label"] == NAME
        assert f"“{NAME}”" in notice["message"]
        assert "apply" in notice["message"] and "cannot send" in notice["message"]
    assert "created" in onboarding.build_notice("created", NAME)["message"]
    assert "couldn't create" in onboarding.build_notice("manual", NAME)["message"]


def test_connections_api_returns_the_notice_exactly_once(monkeypatch):
    from contextlib import contextmanager

    from gateway.access import connections
    from tests.test_clerk_oauth import _settings
    from tests.test_connections_page import _request

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
    request = _request()
    request.session["notice"] = onboarding.build_notice("created", NAME)

    first = connections.connections(request)
    assert first["notice"]["status"] == "created"
    assert connections.connections(request)["notice"] is None  # a reload never repeats it


def _run_callback_helper(monkeypatch, labels, scopes):
    from contextlib import contextmanager

    from gateway.oauth import routes

    conn = SimpleNamespace(connection_metadata={"keep": 1})

    @contextmanager
    def scope():
        yield SimpleNamespace(get=lambda *_: conn)

    monkeypatch.setattr(routes, "session_scope", scope)
    monkeypatch.setattr(
        routes.google_client, "build_gmail_service", lambda _creds: _service(labels)
    )
    request = SimpleNamespace(session={})
    settings = SimpleNamespace(gmail_access_label=NAME)
    routes._onboard_gmail(request, settings, object(), "cid", scopes)
    return request.session, conn


def test_callback_marks_onboarded_only_when_label_exists(monkeypatch):
    session, conn = _run_callback_helper(monkeypatch, _Labels(), GMAIL_SCOPES)
    assert session["notice"]["status"] == "created"
    assert conn.connection_metadata == {"keep": 1, onboarding.ONBOARDED_KEY: True}

    # No label scope -> manual instructions, and the next connect tries again.
    session, conn = _run_callback_helper(monkeypatch, _Labels(), GMAIL_TOOL_SCOPES)
    assert session["notice"]["status"] == "manual"
    assert onboarding.ONBOARDED_KEY not in conn.connection_metadata
