"""Tests for label-scoped Gmail read tools and message parsing."""

from __future__ import annotations

import base64
import uuid
from types import SimpleNamespace

import pytest

from gateway.audit.log import summarize_input
from gateway.providers.base import CallContext
from gateway.providers.google.gmail import common, read
from gateway.providers.registry import ToolError

LABEL = "Label_1"


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


class _Exec:
    def __init__(self, result, sink=None, kwargs=None):
        self._result, self._sink, self._kwargs = result, sink, kwargs

    def execute(self):
        if self._sink is not None:
            self._sink.append(self._kwargs)
        return self._result


class _Resource:
    def __init__(self, handlers):
        self._handlers = handlers
        self.calls: dict[str, list] = {}

    def __getattr__(self, name):
        if name in self._handlers:

            def call(**kwargs):
                sink = self.calls.setdefault(name, [])
                return _Exec(self._handlers[name](**kwargs), sink, kwargs)

            return call
        raise AttributeError(name)


class _Service:
    def __init__(self, labels, messages, threads=None):
        self.msgs = messages
        self.labels_res = _Resource({"list": lambda **k: {"labels": labels}})
        self.messages_res = _Resource(
            {
                "list": lambda **k: {"messages": [{"id": i} for i in self.msgs]},
                "get": lambda **k: self.msgs[k["id"]],
            }
        )
        self.threads_res = _Resource({"get": lambda **k: (threads or {})[k["id"]]})
        self.attachments_res = _Resource({"get": lambda **k: {"data": _b64("PDFBYTES")}})
        self.messages_res.attachments = lambda: self.attachments_res

    def users(self):
        return self

    def labels(self):
        return self.labels_res

    def messages(self):
        return self.messages_res

    def threads(self):
        return self.threads_res


def _msg(mid, labels, subject="Hi"):
    return {
        "id": mid,
        "threadId": "t1",
        "labelIds": labels,
        "snippet": "snip",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": "a@example.com"},
            ],
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": _b64("plain")}},
                        {"mimeType": "text/html", "body": {"data": _b64("<b>html</b>")}},
                    ],
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "a.pdf",
                    "body": {"size": 12, "attachmentId": "att1"},
                },
            ],
        },
    }


@pytest.fixture
def ctx():
    return CallContext(user_id=uuid.uuid4(), external_user_id="u", request_id="r")


def _patch(monkeypatch, service, metadata=None):
    monkeypatch.setattr(
        read,
        "gmail_service_and_connection",
        lambda _s, _c: (service, SimpleNamespace(connection_metadata=metadata or {})),
    )


LABELS = [{"id": LABEL, "name": "Agent-Access"}]


def test_search_always_constrains_label(monkeypatch, ctx):
    svc = _Service(LABELS, {"m1": _msg("m1", [LABEL]), "m2": _msg("m2", [])})
    _patch(monkeypatch, svc)
    out = read.search_messages(read.SearchMessagesInput(query="in:anywhere -label:x"), ctx, None)
    assert svc.messages_res.calls["list"][0]["labelIds"] == [LABEL]
    assert [m["id"] for m in out["messages"]] == ["m1"]  # defensive recheck drops m2


def test_missing_label(monkeypatch, ctx):
    _patch(monkeypatch, _Service([], {}))
    with pytest.raises(ToolError) as e:
        read.search_messages(read.SearchMessagesInput(), ctx, None)
    assert e.value.error_code == "label_not_found"


def test_get_message_unlabelled_is_not_found(monkeypatch, ctx):
    _patch(monkeypatch, _Service(LABELS, {"m": _msg("m", ["INBOX"])}))
    with pytest.raises(ToolError) as e:
        read.get_message(read.GetMessageInput(message_id="m"), ctx, None)
    assert e.value.error_code == "not_found"


def test_get_message_parses_multipart(monkeypatch, ctx):
    _patch(monkeypatch, _Service(LABELS, {"m": _msg("m", [LABEL])}))
    out = read.get_message(read.GetMessageInput(message_id="m"), ctx, None)
    assert out["body_text"] == "plain"
    assert out["body_html"] == "<b>html</b>"
    assert out["attachments"] == [
        {"filename": "a.pdf", "mime_type": "application/pdf", "size": 12, "attachment_id": "att1"}
    ]


def test_get_thread_filters_unlabelled(monkeypatch, ctx):
    thread = {"t": {"messages": [_msg("a", [LABEL]), _msg("b", [])]}}
    _patch(monkeypatch, _Service(LABELS, {}, thread))
    out = read.get_thread(read.GetThreadInput(thread_id="t"), ctx, None)
    assert [m["id"] for m in out["messages"]] == ["a"]
    _patch(monkeypatch, _Service(LABELS, {}, {"t": {"messages": [_msg("b", [])]}}))
    with pytest.raises(ToolError):
        read.get_thread(read.GetThreadInput(thread_id="t"), ctx, None)


def test_audit_drops_query():
    s = summarize_input("google_gmail_search_messages", {"query": "secret", "max_results": 5})
    assert "secret" not in s and "max_results=5" in s


def test_html_to_text():
    assert common.html_to_text("<p>Hello</p><script>x()</script><p>World</p>").split() == [
        "Hello",
        "World",
    ]


def _sharing(**value):
    return {"gmail_sharing": value}


def test_all_mail_mode_has_no_label_filter_and_reads_unlabelled(monkeypatch, ctx):
    svc = _Service(LABELS, {"m1": _msg("m1", ["INBOX"]), "m2": _msg("m2", [])})
    _patch(monkeypatch, svc, _sharing(mode="all"))
    out = read.search_messages(read.SearchMessagesInput(), ctx, None)
    assert "labelIds" not in svc.messages_res.calls["list"][0]
    assert [m["id"] for m in out["messages"]] == ["m1", "m2"]
    msg = read.get_message(read.GetMessageInput(message_id="m1"), ctx, None)
    assert msg["id"] == "m1"


def test_chosen_labels_limit_reads_and_ignore_the_default_label(monkeypatch, ctx):
    labels = [{"id": "L1", "name": "Agent-Access"}, {"id": "L2", "name": "Projects"}]
    svc = _Service(labels, {"a": _msg("a", ["L1"]), "p": _msg("p", ["L2"])})
    _patch(monkeypatch, svc, _sharing(mode="labels", labels=["Projects"]))
    out = read.search_messages(read.SearchMessagesInput(), ctx, None)
    assert svc.messages_res.calls["list"][0]["labelIds"] == ["L2"]
    assert [m["id"] for m in out["messages"]] == ["p"]
    with pytest.raises(ToolError) as e:
        read.get_message(read.GetMessageInput(message_id="a"), ctx, None)
    assert e.value.error_code == "not_found"


def test_several_labels_are_listed_per_label_and_merged(monkeypatch, ctx):
    labels = [{"id": "L1", "name": "A"}, {"id": "L2", "name": "B"}]
    svc = _Service(labels, {"a": _msg("a", ["L1"]), "b": _msg("b", ["L2"]), "c": _msg("c", [])})
    _patch(monkeypatch, svc, _sharing(mode="labels", labels=["A", "B"]))
    out = read.search_messages(read.SearchMessagesInput(), ctx, None)
    assert {c["labelIds"][0] for c in svc.messages_res.calls["list"]} == {"L1", "L2"}
    assert sorted(m["id"] for m in out["messages"]) == ["a", "b"]  # "c" rechecked away
    assert out["next_page_token"] is None


def test_normalize_sharing_validates():
    from gateway.providers.google.gmail import sharing

    assert sharing.normalize("all", None).mode == "all"
    assert sharing.normalize("labels", [" A ", "A", "B"]).labels == ("A", "B")
    for bad in (("labels", []), ("labels", ["", " "]), ("other", ["A"])):
        with pytest.raises(ValueError):
            sharing.normalize(*bad)
    default = sharing.get_sharing({}, "agent-access")
    assert (default.mode, default.labels) == ("labels", ("agent-access",))


def _stage_capture(monkeypatch):
    staged = {}

    def fake_stage(_session, user_id, **kw):
        staged.update(kw, user_id=user_id)
        return "ref-1", __import__("datetime").datetime(2030, 1, 1)

    monkeypatch.setattr(read, "stage", fake_stage)
    return staged


def test_download_attachment_stages_file_and_returns_no_bytes(monkeypatch, ctx):
    _patch(monkeypatch, _Service(LABELS, {"m": _msg("m", [LABEL])}))
    staged = _stage_capture(monkeypatch)
    inp = read.DownloadAttachmentInput(message_id="m", attachment_id="att1")
    out = read.download_attachment(inp, ctx, None)
    assert out["file_ref"] == "ref-1" and out["filename"] == "a.pdf"
    assert out["size"] == len("PDFBYTES") and "data" not in out
    assert staged["data"] == b"PDFBYTES" and staged["user_id"] == ctx.user_id


def test_download_attachment_unshared_message_is_not_found(monkeypatch, ctx):
    _patch(monkeypatch, _Service(LABELS, {"m": _msg("m", ["INBOX"])}))
    _stage_capture(monkeypatch)
    inp = read.DownloadAttachmentInput(message_id="m", attachment_id="att1")
    with pytest.raises(ToolError) as e:
        read.download_attachment(inp, ctx, None)
    assert e.value.error_code == "not_found"


def test_download_attachment_unknown_id_and_too_large(monkeypatch, ctx):
    msg = _msg("m", [LABEL])
    _patch(monkeypatch, _Service(LABELS, {"m": msg}))
    _stage_capture(monkeypatch)
    with pytest.raises(ToolError) as e:
        read.download_attachment(
            read.DownloadAttachmentInput(message_id="m", attachment_id="nope"), ctx, None
        )
    assert e.value.error_code == "not_found"
    msg["payload"]["parts"][1]["body"]["size"] = 11 * 1024 * 1024
    with pytest.raises(ToolError) as e:
        read.download_attachment(
            read.DownloadAttachmentInput(message_id="m", attachment_id="att1"), ctx, None
        )
    assert e.value.error_code == "too_large"


def test_download_attachment_audit_records_ids_only():
    out = summarize_input(
        "google_gmail_download_attachment", {"message_id": "m", "attachment_id": "a", "x": "y"}
    )
    assert "m" in out and "x" not in out
