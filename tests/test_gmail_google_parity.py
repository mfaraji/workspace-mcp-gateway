"""Gmail tools matching Google's MCP set: label/trash/spam guards and the bare-name mount."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from gateway.config import Settings
from gateway.connectors.registry import GOOGLE_GMAIL_TOOLS, by_slug
from gateway.mcp.server import build_mcp, product_tool_filter
from gateway.oauth.google import GMAIL_MODIFY_SCOPE
from gateway.providers.base import CallContext, RiskTier
from gateway.providers.google.gmail import manage, read
from gateway.providers.registry import ToolError, ToolRegistry

SHARE = "Label_share"
LABELS = [
    {"id": SHARE, "name": "Agent-Access", "type": "user"},
    {"id": "Label_work", "name": "Work", "type": "user"},
    {"id": "INBOX", "name": "INBOX", "type": "system"},
    {"id": "STARRED", "name": "STARRED", "type": "system"},
    {"id": "CATEGORY_PROMOTIONS", "name": "CATEGORY_PROMOTIONS", "type": "system"},
    {"id": "SPAM", "name": "SPAM", "type": "system"},
    {"id": "TRASH", "name": "TRASH", "type": "system"},
    {"id": "SENT", "name": "SENT", "type": "system"},
]


class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _Res:
    """Records every call as ``(method, kwargs)`` and answers from ``handlers``."""

    def __init__(self, log, prefix, handlers):
        self._log, self._prefix, self._handlers = log, prefix, handlers

    def __getattr__(self, name):
        def call(**kwargs):
            self._log.append((f"{self._prefix}.{name}", kwargs))
            handler = self._handlers.get(name)
            return _Exec(handler(**kwargs) if handler else {})

        return call


class _Service:
    def __init__(self, messages, threads=None, labels=None):
        self.log: list[tuple[str, dict]] = []
        self._labels = list(labels if labels is not None else LABELS)
        self.labels_res = _Res(
            self.log,
            "labels",
            {
                "list": lambda **k: {"labels": self._labels},
                "create": lambda **k: {"id": "Label_new", "name": k["body"]["name"]},
                "patch": lambda **k: {"id": k["id"], "name": k["body"].get("name", "x")},
            },
        )
        self.messages_res = _Res(self.log, "messages", {"get": lambda **k: messages[k["id"]]})
        self.threads_res = _Res(self.log, "threads", {"get": lambda **k: threads[k["id"]]})

    def users(self):
        return self

    def labels(self):
        return self.labels_res

    def messages(self):
        return self.messages_res

    def threads(self):
        return self.threads_res

    def calls(self, name):
        return [kw for n, kw in self.log if n == name]


def _msg(mid, labels):
    return {"id": mid, "threadId": "t1", "labelIds": labels}


@pytest.fixture
def ctx():
    return CallContext(user_id=uuid.uuid4(), external_user_id="u", request_id="r")


def _patch(monkeypatch, service, *, scopes=(GMAIL_MODIFY_SCOPE,), metadata=None):
    conn = SimpleNamespace(connection_metadata=metadata or {}, scopes=list(scopes))
    monkeypatch.setattr(manage, "gmail_service_and_connection", lambda _s, _c: (service, conn))
    monkeypatch.setattr(read, "gmail_service_and_connection", lambda _s, _c: (service, conn))


def _shared_service(**kw):
    messages = {"m": _msg("m", [SHARE]), "x": _msg("x", ["INBOX"])}
    threads = {
        "t1": {"messages": [_msg("m", [SHARE]), _msg("x", ["INBOX"]), _msg("m2", [SHARE])]},
        "t2": {"messages": [_msg("x", ["INBOX"])]},
    }
    return _Service(messages, threads, **kw)


# --- scope enforcement ------------------------------------------------------------------


def test_unshared_message_is_not_found_and_never_modified(monkeypatch, ctx):
    svc = _shared_service()
    _patch(monkeypatch, svc)
    with pytest.raises(ToolError) as e:
        manage.label_message(
            manage.MessageLabelsInput(message_id="x", label_ids=["Label_work"]), ctx, None
        )
    assert e.value.error_code == "not_found"
    assert svc.calls("messages.modify") == []


def test_all_mail_sharing_allows_any_message(monkeypatch, ctx):
    svc = _shared_service()
    _patch(monkeypatch, svc, metadata={"gmail_sharing": {"mode": "all"}})
    manage.label_message(
        manage.MessageLabelsInput(message_id="x", label_ids=["Label_work"]), ctx, None
    )
    assert svc.calls("messages.modify")[0]["body"]["addLabelIds"] == ["Label_work"]


def test_thread_tools_touch_only_shared_messages(monkeypatch, ctx):
    svc = _shared_service()
    _patch(monkeypatch, svc)
    out = manage.label_thread(
        manage.ThreadLabelsInput(thread_id="t1", label_ids=["Label_work"]), ctx, None
    )
    assert out["messages_changed"] == 2
    assert sorted(c["id"] for c in svc.calls("messages.modify")) == ["m", "m2"]
    trash = manage._trash_thread(False)(manage.ThreadInput(thread_id="t1"), ctx, None)
    assert trash["messages_changed"] == 2
    assert sorted(c["id"] for c in svc.calls("messages.trash")) == ["m", "m2"]
    assert svc.calls("threads.trash") == []  # never the whole-thread call


def test_thread_without_shared_messages_is_not_found(monkeypatch, ctx):
    svc = _shared_service()
    _patch(monkeypatch, svc)
    with pytest.raises(ToolError) as e:
        manage._spam_thread(True)(manage.ThreadInput(thread_id="t2"), ctx, None)
    assert e.value.error_code == "not_found"
    assert svc.calls("messages.modify") == []


def test_trash_untrash_and_spam_use_dedicated_calls(monkeypatch, ctx):
    svc = _shared_service()
    _patch(monkeypatch, svc)
    inp = manage.MessageInput(message_id="m")
    manage._trash_message(False)(inp, ctx, None)
    manage._trash_message(True)(inp, ctx, None)
    manage._spam_message(True)(inp, ctx, None)
    manage._spam_message(False)(inp, ctx, None)
    assert [c["id"] for c in svc.calls("messages.trash")] == ["m"]
    assert [c["id"] for c in svc.calls("messages.untrash")] == ["m"]
    spam, unspam = (c["body"] for c in svc.calls("messages.modify"))
    assert spam == {"addLabelIds": ["SPAM"], "removeLabelIds": ["INBOX"]}
    assert unspam == {"addLabelIds": ["INBOX"], "removeLabelIds": ["SPAM"]}


# --- label guards -------------------------------------------------------------------------


@pytest.mark.parametrize("label_id", [SHARE])
def test_sharing_label_can_never_be_added_or_removed(monkeypatch, ctx, label_id):
    svc = _shared_service()
    _patch(monkeypatch, svc)
    for fn in (manage.label_message, manage.unlabel_message):
        with pytest.raises(ToolError) as e:
            fn(manage.MessageLabelsInput(message_id="m", label_ids=[label_id]), ctx, None)
        assert e.value.error_code == "forbidden"
    with pytest.raises(ToolError) as e:
        manage.update_message_labels(
            manage.UpdateMessageLabelsInput(message_id="m", remove_label_ids=[label_id]),
            ctx,
            None,
        )
    assert e.value.error_code == "forbidden"
    assert svc.calls("messages.modify") == []


@pytest.mark.parametrize("label_id", ["SPAM", "TRASH", "SENT", "DRAFT", "Label_missing"])
def test_disallowed_system_and_unknown_labels_are_rejected(monkeypatch, ctx, label_id):
    svc = _shared_service()
    _patch(monkeypatch, svc)
    with pytest.raises(ToolError) as e:
        manage.label_message(
            manage.MessageLabelsInput(message_id="m", label_ids=[label_id]), ctx, None
        )
    assert e.value.error_code == "invalid_input"


def test_allowed_system_labels_work(monkeypatch, ctx):
    svc = _shared_service()
    _patch(monkeypatch, svc)
    manage.update_message_labels(
        manage.UpdateMessageLabelsInput(
            message_id="m",
            add_label_ids=["STARRED", "CATEGORY_PROMOTIONS"],
            remove_label_ids=["INBOX"],
        ),
        ctx,
        None,
    )
    assert svc.calls("messages.modify")[0]["body"] == {
        "addLabelIds": ["STARRED", "CATEGORY_PROMOTIONS"],
        "removeLabelIds": ["INBOX"],
    }


def test_label_crud_protects_sharing_label_and_system_labels(monkeypatch, ctx):
    svc = _shared_service()
    _patch(monkeypatch, svc)
    for label_id, code in ((SHARE, "forbidden"), ("INBOX", "invalid_input"), ("nope", "not_found")):
        with pytest.raises(ToolError) as e:
            manage.delete_label(manage.DeleteLabelInput(label_id=label_id), ctx, None)
        assert e.value.error_code == code
        with pytest.raises(ToolError) as e:
            manage.update_label(manage.UpdateLabelInput(label_id=label_id, name="Z"), ctx, None)
        assert e.value.error_code == code
    assert svc.calls("labels.delete") == [] and svc.calls("labels.patch") == []
    with pytest.raises(ToolError) as e:
        manage.update_label(
            manage.UpdateLabelInput(label_id="Label_work", name="agent-ACCESS"), ctx, None
        )
    assert e.value.error_code == "forbidden"
    with pytest.raises(ToolError) as e:
        manage.create_label(manage.CreateLabelInput(name=" agent-access "), ctx, None)
    assert e.value.error_code == "forbidden"
    manage.delete_label(manage.DeleteLabelInput(label_id="Label_work"), ctx, None)
    assert svc.calls("labels.delete")[0]["id"] == "Label_work"
    assert (
        manage.create_label(manage.CreateLabelInput(name="Receipts"), ctx, None)["label_id"]
        == "Label_new"
    )


def test_sensitive_label_is_created_on_demand_and_applied(monkeypatch, ctx):
    svc = _shared_service()
    _patch(monkeypatch, svc)
    out = manage.apply_sensitive_message_label(manage.MessageInput(message_id="m"), ctx, None)
    assert out["label_id"] == "Label_new"
    assert svc.calls("labels.create")[0]["body"]["name"] == "Sensitive"
    assert svc.calls("messages.modify")[0]["body"]["addLabelIds"] == ["Label_new"]


def test_management_tools_ask_to_reconnect_without_modify_scope(monkeypatch, ctx):
    svc = _shared_service()
    _patch(monkeypatch, svc, scopes=["https://www.googleapis.com/auth/gmail.readonly"])
    monkeypatch.setattr(
        "gateway.providers.google.gmail.common.GMAIL_OAUTH.connect_url",
        lambda *_a: "https://gw/connect",
    )
    with pytest.raises(ToolError) as e:
        manage._trash_message(False)(manage.MessageInput(message_id="m"), ctx, None)
    assert e.value.error_code == "reauth_required"
    assert svc.calls("messages.trash") == []


# --- read additions -----------------------------------------------------------------------


def test_list_labels_marks_sharing_label(monkeypatch, ctx):
    _patch(monkeypatch, _shared_service())
    out = read.list_labels(read.ListLabelsInput(), ctx, None)
    shared = {label["id"] for label in out["labels"] if label["shared"]}
    assert shared == {SHARE}


def test_search_threads_returns_only_shared_messages(monkeypatch, ctx):
    svc = _shared_service()
    svc.threads_res._handlers["list"] = lambda **k: {"threads": [{"id": "t1"}, {"id": "t2"}]}
    _patch(
        monkeypatch, svc, metadata={"gmail_sharing": {"mode": "labels", "labels": ["Agent-Access"]}}
    )
    out = read.search_threads(read.SearchThreadsInput(query="receipt"), ctx, None)
    assert svc.calls("threads.list")[0]["labelIds"] == [SHARE]
    assert [t["thread_id"] for t in out["threads"]] == ["t1"]
    assert [m["id"] for m in out["threads"][0]["messages"]] == ["m", "m2"]


# --- tiers, registration, mount ------------------------------------------------------------

LOW = {
    "create_label", "label_message", "unlabel_message", "update_message_labels", "label_thread",
    "unlabel_thread", "untrash_message", "untrash_thread", "unmark_message_spam",
    "unmark_thread_spam", "apply_sensitive_message_label", "apply_sensitive_thread_label",
}  # fmt: skip
MEDIUM = {
    "update_label", "delete_label", "trash_message", "trash_thread", "mark_message_spam",
    "mark_thread_spam",
}  # fmt: skip


def test_tiers_and_previews():
    registry = ToolRegistry()
    manage.register(registry)
    specs = {s.name.removeprefix("google_gmail_"): s for s in registry._specs}
    assert set(specs) == LOW | MEDIUM
    for name in LOW:
        assert specs[name].default_tier is RiskTier.LOW
    for name in MEDIUM:
        assert specs[name].default_tier is RiskTier.MEDIUM
    for spec in specs.values():
        assert spec.preview_builder is not None
        assert "send" not in spec.name


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
        base_url="http://localhost:8000",
        google_client_id="cid",
        google_client_secret="secret",
        token_encryption_key="x" * 43 + "=",
        gateway_shared_secret="shared-secret-value",
        trusted_open_webui_origin="https://openwebui.internal",
        session_secret="sess",
    )


def test_gmail_mount_exposes_exactly_googles_tools_under_bare_names():
    connector = by_slug("gmail")
    mcp = build_mcp(_settings(), product_tool_filter("gmail"), connector=connector)
    names = set(mcp._tool_manager._tools)
    expected = {*GOOGLE_GMAIL_TOOLS, "download_gmail_attachment", "system_get_current_time"}
    assert names == expected
    assert not {"send_message", "reply", "forward", "list_accounts", "search_messages"} & names
    # A call by bare name runs the internal spec, so grants/tiers/audit use the internal name.
    assert mcp.gateway_exposed["get_message"].name == "google_gmail_get_message"
    assert mcp.gateway_exposed["download_gmail_attachment"].name == (
        "google_gmail_download_attachment"
    )
    assert mcp._tool_manager._tools["get_message"].fn.__name__ == "google_gmail_get_message"


def test_non_connector_builds_keep_prefixed_names_and_extras():
    names = set(build_mcp(_settings(), product_tool_filter("gmail"))._tool_manager._tools)
    assert {"google_gmail_list_accounts", "google_gmail_search_messages"} <= names
    assert "google_gmail_trash_thread" in names
    assert not any("send" in n for n in names)
