"""Google-parity Drive tools: reads, writes, tiers, audit fields and the bare-name mount."""

from __future__ import annotations

import datetime
import uuid
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError
from pydantic import ValidationError

from gateway.audit.log import summarize_input
from gateway.config import Settings
from gateway.connectors.registry import GOOGLE_DRIVE_TOOLS, by_slug
from gateway.mcp.server import build_mcp, product_tool_filter
from gateway.providers.base import CallContext, RiskLevel, RiskTier
from gateway.providers.google.drive import common, read, write
from gateway.providers.registry import ToolError, ToolRegistry
from gateway.storage import staged_files

FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"


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


@pytest.fixture
def ctx():
    return CallContext(user_id=uuid.uuid4(), external_user_id="u", request_id="r")


class _Req:
    def __init__(self, result):
        self.result = result

    def execute(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _Resource:
    """Records every call and answers from ``responses[method]`` (a value or callable)."""

    def __init__(self, responses):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, method):
        def call(**kwargs):
            self.calls.append((method, kwargs))
            answer = self.responses.get(method, {})
            return _Req(answer(**kwargs) if callable(answer) else answer)

        return call

    def last(self, method):
        return [kw for m, kw in self.calls if m == method][-1]


class _Service:
    def __init__(self, files=None, permissions=None, drives=None, revisions=None):
        self._r = {
            "files": _Resource(files or {}),
            "permissions": _Resource(permissions or {}),
            "drives": _Resource(drives or {}),
            "revisions": _Resource(revisions or {}),
        }

    def __getattr__(self, name):
        return lambda: self._r[name]


def _patch_read(monkeypatch, service):
    monkeypatch.setattr(read, "drive_service", lambda _s, _c: service)


def _patch_write(monkeypatch, service):
    monkeypatch.setattr(write, "drive_write_service", lambda _s, _c: service)


def _http_error(status):
    return HttpError(
        SimpleNamespace(status=status, reason="x"), b"{}", uri="https://google.invalid"
    )


def _file(fid="f1", mime="text/plain", size="5", name="n.txt"):
    return {"id": fid, "name": name, "mimeType": mime, "size": size}


# --- registration, tiers, audit ------------------------------------------------------------


def test_tools_registered_with_expected_risk_and_tiers():
    registry = ToolRegistry()
    read.register(registry)
    write.register(registry)
    specs = {s.name: s for s in registry._specs}
    for name in GOOGLE_DRIVE_TOOLS:
        assert f"google_drive_{name}" in specs, name

    for name in (
        "download_file_content", "get_file_permissions", "get_shared_drive_metadata",
        "list_file_revisions", "list_recent_files", "list_shared_drives",
        "read_file_content", "search_files", "get_file_metadata",
    ):  # fmt: skip
        assert specs[f"google_drive_{name}"].risk is RiskLevel.READ
    for name in ("share_file", "request_access", "trash_file", "update_file"):
        assert specs[f"google_drive_{name}"].default_tier is RiskTier.MEDIUM
    for name in ("create_file", "copy_file"):
        assert specs[f"google_drive_{name}"].default_tier is RiskTier.LOW
    for name in ("create_file", "copy_file", "update_file", "trash_file", "share_file",
                 "request_access"):  # fmt: skip
        assert specs[f"google_drive_{name}"].preview_builder is not None


def test_audit_records_ids_and_enums_only():
    share = summarize_input(
        "google_drive_share_file",
        {"file_id": "f", "email": "a@b.com", "role": "writer", "type": "user", "message": "hi"},
    )
    assert "f" in share and "writer" in share
    assert "a@b.com" not in share and "hi" not in share
    create = summarize_input(
        "google_drive_create_file", {"name": "secret.txt", "content": "body", "parent_id": "p"}
    )
    assert "p" in create and "secret" not in create and "body" not in create
    assert summarize_input("google_drive_read_file_content", {"file_id": "x"}) == "file_id='x'"


# --- the production mount -------------------------------------------------------------------


def test_drive_mount_exposes_exactly_googles_tools_under_bare_names():
    mcp = build_mcp(_settings(), product_tool_filter("drive"), connector=by_slug("drive"))
    names = set(mcp._tool_manager._tools)
    assert names == set(GOOGLE_DRIVE_TOOLS) | {"system_get_current_time"}
    assert len(GOOGLE_DRIVE_TOOLS) == 15
    assert "google_drive_list_accounts" not in names


def test_bare_names_route_to_the_internal_spec():
    mcp = build_mcp(_settings(), product_tool_filter("drive"), connector=by_slug("drive"))
    for bare in GOOGLE_DRIVE_TOOLS:
        assert mcp._tool_manager._tools[bare].fn.__name__ == f"google_drive_{bare}"
        assert mcp.gateway_exposed[bare].name == f"google_drive_{bare}"
    assert mcp.gateway_exposed["share_file"].default_tier is RiskTier.MEDIUM


def test_all_endpoint_keeps_prefixed_names():
    names = set(build_mcp(_settings())._tool_manager._tools)
    assert "google_drive_share_file" in names and "share_file" not in names


# --- read tools -----------------------------------------------------------------------------


def test_list_recent_files_orders_by_recency(monkeypatch, ctx):
    svc = _Service(files={"list": {"files": [_file()], "nextPageToken": "t"}})
    _patch_read(monkeypatch, svc)
    out = read.list_recent_files(read.ListRecentFilesInput(page_size=5), ctx, None)
    assert out["next_page_token"] == "t" and out["files"][0]["id"] == "f1"
    kw = svc._r["files"].last("list")
    assert kw["orderBy"] == "recency desc" and kw["pageSize"] == 5 and kw["supportsAllDrives"]


def test_get_file_permissions_drops_deleted(monkeypatch, ctx):
    perms = [
        {"id": "1", "type": "user", "role": "writer", "emailAddress": "a@b.com"},
        {"id": "2", "type": "anyone", "role": "reader", "deleted": True},
    ]
    _patch_read(monkeypatch, _Service(permissions={"list": {"permissions": perms}}))
    out = read.get_file_permissions(read.FileIdInput(file_id="f"), ctx, None)
    assert [p["id"] for p in out["permissions"]] == ["1"]
    assert out["permissions"][0]["email_address"] == "a@b.com"


def test_shared_drive_tools(monkeypatch, ctx):
    svc = _Service(
        drives={
            "get": {"id": "d1", "name": "Team", "createdTime": "t", "capabilities": {"a": 1}},
            "list": {"drives": [{"id": "d1", "name": "Team"}], "nextPageToken": "n"},
        }
    )
    _patch_read(monkeypatch, svc)
    meta = read.get_shared_drive_metadata(read.SharedDriveInput(drive_id="d1"), ctx, None)
    assert meta["name"] == "Team" and meta["capabilities"] == {"a": 1}
    listed = read.list_shared_drives(read.ListSharedDrivesInput(), ctx, None)
    assert listed["drives"][0]["id"] == "d1" and listed["next_page_token"] == "n"


def test_list_file_revisions_shapes_output(monkeypatch, ctx):
    rev = {
        "id": "r1",
        "modifiedTime": "t",
        "size": "12",
        "lastModifyingUser": {"displayName": "Ann", "emailAddress": "a@b.com"},
    }
    _patch_read(monkeypatch, _Service(revisions={"list": {"revisions": [rev]}}))
    out = read.list_file_revisions(read.ListRevisionsInput(file_id="f"), ctx, None)
    assert out["revisions"][0] == {
        "id": "r1", "modified_time": "t", "mime_type": None, "size": 12,
        "keep_forever": None, "modified_by": "Ann",
    }  # fmt: skip


def test_read_file_content_exports_docs_as_text_and_truncates(monkeypatch, ctx):
    big = ("x" * (read.MAX_READ_CHARS + 10)).encode()
    svc = _Service(files={"get": _file("d", DOC, None, "Doc"), "export_media": big})
    _patch_read(monkeypatch, svc)
    out = read.read_file_content(read.FileIdInput(file_id="d"), ctx, None)
    assert svc._r["files"].last("export_media")["mimeType"] == "text/plain"
    assert out["truncated"] is True and len(out["content"]) == read.MAX_READ_CHARS


def test_read_file_content_sheet_as_csv_and_text_file(monkeypatch, ctx):
    sheet = "application/vnd.google-apps.spreadsheet"
    svc = _Service(files={"get": _file("s", sheet, None), "export_media": b"a,b\n1,2"})
    _patch_read(monkeypatch, svc)
    out = read.read_file_content(read.FileIdInput(file_id="s"), ctx, None)
    assert svc._r["files"].last("export_media")["mimeType"] == "text/csv"
    assert out["content"] == "a,b\n1,2" and out["truncated"] is False
    svc = _Service(files={"get": _file("t"), "get_media": b"hello"})
    _patch_read(monkeypatch, svc)
    assert read.read_file_content(read.FileIdInput(file_id="t"), ctx, None)["content"] == "hello"


def test_read_file_content_refuses_binary_and_large(monkeypatch, ctx):
    _patch_read(monkeypatch, _Service(files={"get": _file("p", "application/pdf")}))
    with pytest.raises(ToolError) as e:
        read.read_file_content(read.FileIdInput(file_id="p"), ctx, None)
    assert e.value.error_code == "unsupported_type"
    big = _file("t", size=str(read.MAX_READ_BYTES + 1))
    _patch_read(monkeypatch, _Service(files={"get": big}))
    with pytest.raises(ToolError) as e:
        read.read_file_content(read.FileIdInput(file_id="t"), ctx, None)
    assert e.value.error_code == "too_large"


def test_read_errors_are_classified(monkeypatch, ctx):
    _patch_read(monkeypatch, _Service(files={"get": _http_error(404)}))
    with pytest.raises(ToolError) as e:
        read.read_file_content(read.FileIdInput(file_id="x"), ctx, None)
    assert e.value.error_code == "not_found"


def _stage_capture(monkeypatch):
    staged = {}

    def fake_stage(_session, user_id, **kw):
        staged.update(kw, user_id=user_id)
        return "ref-1", datetime.datetime(2030, 1, 1)

    monkeypatch.setattr(read, "stage", fake_stage)
    return staged


def test_download_file_content_stages_bytes_and_returns_no_data(monkeypatch, ctx):
    svc = _Service(files={"get": _file("f", "application/pdf", "7", "a.pdf")})
    _patch_read(monkeypatch, svc)
    monkeypatch.setattr(common, "_download_request", lambda req, out: out.write(b"PDFDATA"))
    staged = _stage_capture(monkeypatch)
    out = read.download_file_content(read.FileIdInput(file_id="f"), ctx, None)
    assert out["file_ref"] == "ref-1" and out["size"] == 7 and "data" not in out
    assert staged["data"] == b"PDFDATA" and staged["user_id"] == ctx.user_id
    assert staged["filename"] == "a.pdf" and staged["source"] == "drive"


def test_download_file_content_enforces_10mb_cap(monkeypatch, ctx):
    big = _file("f", "application/pdf", str(staged_files.MAX_FILE_BYTES + 1))
    _patch_read(monkeypatch, _Service(files={"get": big}))
    _stage_capture(monkeypatch)
    with pytest.raises(ToolError) as e:
        read.download_file_content(read.FileIdInput(file_id="f"), ctx, None)
    assert e.value.error_code == "too_large"
    # An export with no advertised size is checked after download.
    _patch_read(monkeypatch, _Service(files={"get": _file("d", DOC, None)}))
    monkeypatch.setattr(
        common,
        "_download_request",
        lambda req, out: out.write(b"0" * (staged_files.MAX_FILE_BYTES + 1)),
    )
    with pytest.raises(ToolError) as e:
        read.download_file_content(read.FileIdInput(file_id="d"), ctx, None)
    assert e.value.error_code == "too_large"


# --- write tools ----------------------------------------------------------------------------


def test_create_file_text_content(monkeypatch, ctx):
    svc = _Service(files={"create": _file("new", "text/plain", "4", "a.txt")})
    _patch_write(monkeypatch, svc)
    out = write.create_file(
        write.CreateFileInput(name="a.txt", content="text", parent_id="p", description="d"),
        ctx,
        None,
    )
    kw = svc._r["files"].last("create")
    assert out["id"] == "new" and kw["body"]["parents"] == ["p"] and kw["body"]["name"] == "a.txt"
    assert kw["body"]["mimeType"] == "text/plain" and kw["media_body"] is not None


def test_create_file_folder_and_conversion(monkeypatch, ctx):
    svc = _Service(files={"create": _file("n", FOLDER, None, "F")})
    _patch_write(monkeypatch, svc)
    write.create_file(write.CreateFileInput(name="F", mime_type=FOLDER), ctx, None)
    kw = svc._r["files"].last("create")
    assert kw["body"]["mimeType"] == FOLDER and "media_body" not in kw
    with pytest.raises(ToolError):
        write.create_file(write.CreateFileInput(name="F", mime_type=FOLDER, content="x"), ctx, None)
    write.create_file(write.CreateFileInput(name="D", content="hi", mime_type=DOC), ctx, None)
    kw = svc._r["files"].last("create")
    assert kw["body"]["mimeType"] == DOC and kw["media_body"] is not None


def test_create_file_from_staged_file_ref(monkeypatch, ctx):
    svc = _Service(files={"create": _file("n", "application/pdf", "4", "r.pdf")})
    _patch_write(monkeypatch, svc)
    monkeypatch.setattr(
        write,
        "take",
        lambda _s, uid, ref: staged_files.StagedContent("r.pdf", "application/pdf", b"%PDF", "x"),
    )
    write.create_file(write.CreateFileInput(name="r.pdf", file_ref="ref"), ctx, None)
    kw = svc._r["files"].last("create")
    assert kw["body"]["mimeType"] == "application/pdf" and kw["media_body"] is not None
    with pytest.raises(ValidationError):
        write.CreateFileInput(name="r", content="x", file_ref="ref")


def test_create_file_size_cap(ctx):
    big = "0" * (staged_files.MAX_FILE_BYTES + 1)
    with pytest.raises(ToolError) as e:
        write._content_bytes(None, ctx, big, None, None)
    assert e.value.error_code == "too_large"


def test_copy_file(monkeypatch, ctx):
    svc = _Service(files={"copy": _file("c")})
    _patch_write(monkeypatch, svc)
    write.copy_file(write.CopyFileInput(file_id="f", name="Copy", parent_id="p"), ctx, None)
    kw = svc._r["files"].last("copy")
    assert kw["fileId"] == "f" and kw["body"] == {"name": "Copy", "parents": ["p"]}


def test_update_file_rename_move_and_validation(monkeypatch, ctx):
    svc = _Service(files={"get": {"parents": ["old1", "old2"]}, "update": _file("f")})
    _patch_write(monkeypatch, svc)
    write.update_file(write.UpdateFileInput(file_id="f", name="N", parent_id="new"), ctx, None)
    kw = svc._r["files"].last("update")
    assert kw["body"] == {"name": "N"}
    assert kw["addParents"] == "new" and kw["removeParents"] == "old1,old2"
    assert "trashed" not in kw["body"]
    with pytest.raises(ValidationError):
        write.UpdateFileInput(file_id="f")
    with pytest.raises(ValidationError):
        write.UpdateFileInput(file_id="f", content="a", file_ref="b")


def test_update_file_replaces_content(monkeypatch, ctx):
    svc = _Service(files={"update": _file("f")})
    _patch_write(monkeypatch, svc)
    write.update_file(write.UpdateFileInput(file_id="f", content="new"), ctx, None)
    assert svc._r["files"].last("update")["media_body"] is not None


def test_trash_file_only_trashes(monkeypatch, ctx):
    svc = _Service(files={"update": {"id": "f", "trashed": True}})
    _patch_write(monkeypatch, svc)
    out = write.trash_file(write.TrashFileInput(file_id="f"), ctx, None)
    assert out == {"id": "f", "trashed": True}
    assert svc._r["files"].last("update")["body"] == {"trashed": True}
    assert not [m for m, _ in svc._r["files"].calls if m == "delete"]


def test_share_file_grants_named_principal_only(monkeypatch, ctx):
    svc = _Service(
        permissions={
            "create": {"id": "p1", "type": "user", "role": "writer", "emailAddress": "a@b.com"}
        }
    )
    _patch_write(monkeypatch, svc)
    args = write.ShareFileInput(file_id="f", email=" a@b.com ", role="writer", message="hello")
    out = write.share_file(args, ctx, None)
    kw = svc._r["permissions"].last("create")
    assert kw["body"] == {"type": "user", "role": "writer", "emailAddress": "a@b.com"}
    assert kw["sendNotificationEmail"] is True and kw["emailMessage"] == "hello"
    assert "transferOwnership" not in kw
    assert out["permission_id"] == "p1"
    preview = write._share_preview(args)
    assert "a@b.com" in preview and "writer" in preview


def test_share_file_rejects_link_domain_owner_and_bad_email():
    base = {"file_id": "f", "email": "a@b.com", "role": "reader"}
    for bad in (
        {"role": "owner"},
        {"role": "organizer"},
        {"type": "anyone"},
        {"type": "domain"},
        {"email": "not-an-email"},
        {"email": "a@b.com, c@d.com"},
    ):
        with pytest.raises(ValidationError):
            write.ShareFileInput(**{**base, **bad})


def test_share_file_without_notification_omits_message(monkeypatch, ctx):
    svc = _Service(permissions={"create": {"id": "p"}})
    _patch_write(monkeypatch, svc)
    args = write.ShareFileInput(
        file_id="f", email="a@b.com", role="reader", send_notification=False, message="x"
    )
    write.share_file(args, ctx, None)
    kw = svc._r["permissions"].last("create")
    assert kw["sendNotificationEmail"] is False and "emailMessage" not in kw


def test_request_access_reports_link_when_inaccessible(monkeypatch, ctx):
    _patch_write(monkeypatch, _Service(files={"get": _http_error(404)}))
    out = write.request_access(write.RequestAccessInput(file_id="f"), ctx, None)
    assert out["status"] == "manual_action_required" and out["url"].endswith("/f/view")
    _patch_write(monkeypatch, _Service(files={"get": {"id": "f"}}))
    assert (
        write.request_access(write.RequestAccessInput(file_id="f"), ctx, None)["status"]
        == "already_has_access"
    )


def test_write_errors_are_classified(monkeypatch, ctx):
    _patch_write(monkeypatch, _Service(files={"update": _http_error(403)}))
    with pytest.raises(ToolError) as e:
        write.trash_file(write.TrashFileInput(file_id="f"), ctx, None)
    assert e.value.error_code == "forbidden"


# --- scopes ---------------------------------------------------------------------------------


def _conn(scopes):
    return SimpleNamespace(scopes=scopes)


def test_write_service_requires_full_drive_scope(monkeypatch, ctx):
    from gateway.oauth.google import DRIVE_READ_SCOPES, DRIVE_WRITE_SCOPE

    monkeypatch.setattr(common, "get_settings", _settings)
    monkeypatch.setattr(common, "build_drive_service", lambda creds: "svc")
    oauth = common.DRIVE_OAUTH
    monkeypatch.setattr(
        oauth, "get_connection_credentials", lambda *a: ("creds", _conn(list(DRIVE_READ_SCOPES)))
    )
    with pytest.raises(ToolError) as e:
        common.drive_write_service(None, ctx)
    assert e.value.error_code == "reauth_required"
    monkeypatch.setattr(
        oauth,
        "get_connection_credentials",
        lambda *a: ("creds", _conn([*DRIVE_READ_SCOPES, DRIVE_WRITE_SCOPE])),
    )
    assert common.drive_write_service(None, ctx) == "svc"


def test_read_only_connections_still_pass_the_read_scope_check():
    from gateway.oauth.google import DRIVE_READ_SCOPES

    assert common.required_scopes_present(list(DRIVE_READ_SCOPES))
    assert common.DRIVE_OAUTH.required_scopes == DRIVE_READ_SCOPES
