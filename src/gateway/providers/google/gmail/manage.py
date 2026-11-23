"""Gmail label, trash and spam tools, hard-scoped to the mail the user shares.

Every write fetches the message first and treats unshared mail exactly like missing mail.
Thread tools act on the *shared* messages of a thread only, one message at a time, so an
unshared reply is never touched. Nothing here sends mail. The sharing label(s) are
read-only to the agent: it must not widen, narrow or rename its own access.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator
from sqlalchemy.orm import Session

from gateway.config import get_settings
from gateway.oauth.google import GMAIL_LABEL_SCOPE, GMAIL_MODIFY_SCOPE
from gateway.providers.base import CallContext, RiskLevel, ToolSpec
from gateway.providers.google.common import GoogleAccountInput
from gateway.providers.google.gmail.common import (
    PROVIDER,
    Scope,
    gmail_service_and_connection,
    in_scope,
    require_in_scope,
    resolve_scope,
)
from gateway.providers.registry import ToolError

# System labels an agent may toggle. SPAM/TRASH have dedicated tools; DRAFT/SENT are not
# user-togglable.
_ALLOWED_SYSTEM = {"INBOX", "UNREAD", "STARRED", "IMPORTANT"}
_MAX_LABEL_IDS = 20
_LABEL_VISIBILITY = {"labelShow", "labelShowIfUnread", "labelHide"}
_MESSAGE_VISIBILITY = {"show", "hide"}


def _ids(values: list[str]) -> list[str]:
    cleaned = list(dict.fromkeys(v.strip() for v in values if v and v.strip()))
    if len(cleaned) > _MAX_LABEL_IDS:
        raise ValueError(f"at most {_MAX_LABEL_IDS} label ids")
    return cleaned


class MessageLabelsInput(GoogleAccountInput):
    message_id: str = Field(..., min_length=1, max_length=255)
    label_ids: list[str] = Field(..., min_length=1, description="Gmail label ids (list_labels).")

    _v = field_validator("label_ids")(lambda cls, v: _ids(v))


class ThreadLabelsInput(GoogleAccountInput):
    thread_id: str = Field(..., min_length=1, max_length=255)
    label_ids: list[str] = Field(..., min_length=1, description="Gmail label ids (list_labels).")

    _v = field_validator("label_ids")(lambda cls, v: _ids(v))


class UpdateMessageLabelsInput(GoogleAccountInput):
    message_id: str = Field(..., min_length=1, max_length=255)
    add_label_ids: list[str] = Field(default_factory=list)
    remove_label_ids: list[str] = Field(default_factory=list)

    _v = field_validator("add_label_ids", "remove_label_ids")(lambda cls, v: _ids(v))


class MessageInput(GoogleAccountInput):
    message_id: str = Field(..., min_length=1, max_length=255)


class ThreadInput(GoogleAccountInput):
    thread_id: str = Field(..., min_length=1, max_length=255)


class CreateLabelInput(GoogleAccountInput):
    name: str = Field(..., min_length=1, max_length=225)
    label_list_visibility: str | None = Field(
        None, description="labelShow|labelShowIfUnread|labelHide"
    )
    message_list_visibility: str | None = Field(None, description="show|hide")


class UpdateLabelInput(GoogleAccountInput):
    label_id: str = Field(..., min_length=1, max_length=255)
    name: str | None = Field(None, min_length=1, max_length=225)
    label_list_visibility: str | None = Field(
        None, description="labelShow|labelShowIfUnread|labelHide"
    )
    message_list_visibility: str | None = Field(None, description="show|hide")


class DeleteLabelInput(GoogleAccountInput):
    label_id: str = Field(..., min_length=1, max_length=255)


def _service(
    session: Session, ctx: CallContext, *, scopes: tuple[str, ...] = (GMAIL_MODIFY_SCOPE,)
):
    """``(service, connection, scope)``; asks for reconnect when none of ``scopes`` was granted."""
    service, conn = gmail_service_and_connection(session, ctx)
    if not set(scopes) & set(conn.scopes or []):
        from gateway.providers.google.gmail.common import GMAIL_OAUTH

        raise ToolError(
            "reauth_required",
            "Gmail label/trash/spam tools need extra Google permission; reconnect here: "
            f"{GMAIL_OAUTH.connect_url(get_settings(), ctx.external_user_id)}",
        )
    return service, conn, resolve_scope(service, conn)


def _labels(service) -> list[dict]:
    return service.users().labels().list(userId="me").execute().get("labels", [])


def _protected(scope: Scope) -> set[str]:
    """Sharing label ids: the agent may never add, remove, rename or delete them."""
    return set() if scope.all_mail else set(scope.label_ids)


def _check_label_ids(service, scope: Scope, ids: list[str]) -> None:
    by_id = {label["id"]: label for label in _labels(service)}
    protected = _protected(scope)
    for label_id in ids:
        if label_id in protected:
            raise ToolError(
                "forbidden", "that label controls what you share; only the user can change it"
            )
        label = by_id.get(label_id)
        if label is None:
            raise ToolError("invalid_input", f"unknown label id '{label_id}'; call list_labels")
        if label.get("type") == "system" and not (
            label_id in _ALLOWED_SYSTEM or label_id.startswith("CATEGORY_")
        ):
            raise ToolError(
                "invalid_input",
                f"system label {label_id} cannot be changed here (use the trash/spam tools)",
            )


def _check_user_label(service, scope: Scope, label_id: str) -> dict:
    by_id = {label["id"]: label for label in _labels(service)}
    label = by_id.get(label_id)
    if label is None:
        raise ToolError("not_found", "label not found")
    if label_id in _protected(scope):
        raise ToolError(
            "forbidden", "that label controls what you share; only the user can change it"
        )
    if label.get("type") != "user":
        raise ToolError("invalid_input", "system labels cannot be changed")
    return label


def _shared_message(service, scope: Scope, message_id: str) -> dict:
    msg = service.users().messages().get(userId="me", id=message_id, format="minimal").execute()
    require_in_scope(msg, scope)
    return msg


def _shared_thread_messages(service, scope: Scope, thread_id: str) -> list[dict]:
    thread = service.users().threads().get(userId="me", id=thread_id, format="minimal").execute()
    shared = [m for m in thread.get("messages", []) if in_scope(m, scope)]
    if not shared:
        raise ToolError("not_found", "thread not found")
    return shared


def _modify(service, message_id: str, add: list[str], remove: list[str]) -> None:
    body = {"addLabelIds": add, "removeLabelIds": remove}
    service.users().messages().modify(userId="me", id=message_id, body=body).execute()


# -- labels on messages and threads ---------------------------------------------------------


def _change_message(args, ctx, session, add: list[str], remove: list[str], status: str) -> dict:
    service, _conn, scope = _service(session, ctx)
    _check_label_ids(service, scope, [*add, *remove])
    _shared_message(service, scope, args.message_id)
    _modify(service, args.message_id, add, remove)
    return {"status": status, "message_id": args.message_id, "added": add, "removed": remove}


def _change_thread(args, ctx, session, add: list[str], remove: list[str], status: str) -> dict:
    service, _conn, scope = _service(session, ctx)
    _check_label_ids(service, scope, [*add, *remove])
    shared = _shared_thread_messages(service, scope, args.thread_id)
    for msg in shared:
        _modify(service, msg["id"], add, remove)
    return {"status": status, "thread_id": args.thread_id, "messages_changed": len(shared)}


def label_message(args: MessageLabelsInput, ctx: CallContext, session: Session) -> dict:
    return _change_message(args, ctx, session, args.label_ids, [], "labels_added")


def unlabel_message(args: MessageLabelsInput, ctx: CallContext, session: Session) -> dict:
    return _change_message(args, ctx, session, [], args.label_ids, "labels_removed")


def update_message_labels(
    args: UpdateMessageLabelsInput, ctx: CallContext, session: Session
) -> dict:
    if not (args.add_label_ids or args.remove_label_ids):
        raise ToolError("invalid_input", "provide add_label_ids and/or remove_label_ids")
    return _change_message(
        args, ctx, session, args.add_label_ids, args.remove_label_ids, "labels_updated"
    )


def label_thread(args: ThreadLabelsInput, ctx: CallContext, session: Session) -> dict:
    return _change_thread(args, ctx, session, args.label_ids, [], "labels_added")


def unlabel_thread(args: ThreadLabelsInput, ctx: CallContext, session: Session) -> dict:
    return _change_thread(args, ctx, session, [], args.label_ids, "labels_removed")


# -- trash and spam --------------------------------------------------------------------------


def _trash_message(untrash: bool):
    def handler(args: MessageInput, ctx: CallContext, session: Session) -> dict:
        service, _conn, scope = _service(session, ctx)
        _shared_message(service, scope, args.message_id)
        messages = service.users().messages()
        (messages.untrash if untrash else messages.trash)(userId="me", id=args.message_id).execute()
        return {"status": "untrashed" if untrash else "trashed", "message_id": args.message_id}

    return handler


def _trash_thread(untrash: bool):
    def handler(args: ThreadInput, ctx: CallContext, session: Session) -> dict:
        service, _conn, scope = _service(session, ctx)
        shared = _shared_thread_messages(service, scope, args.thread_id)
        messages = service.users().messages()
        for msg in shared:
            (messages.untrash if untrash else messages.trash)(userId="me", id=msg["id"]).execute()
        return {
            "status": "untrashed" if untrash else "trashed",
            "thread_id": args.thread_id,
            "messages_changed": len(shared),
        }

    return handler


def _spam_message(spam: bool):
    def handler(args: MessageInput, ctx: CallContext, session: Session) -> dict:
        service, _conn, scope = _service(session, ctx)
        _shared_message(service, scope, args.message_id)
        add, remove = (["SPAM"], ["INBOX"]) if spam else (["INBOX"], ["SPAM"])
        _modify(service, args.message_id, add, remove)
        return {"status": "marked_spam" if spam else "unmarked_spam", "message_id": args.message_id}

    return handler


def _spam_thread(spam: bool):
    def handler(args: ThreadInput, ctx: CallContext, session: Session) -> dict:
        service, _conn, scope = _service(session, ctx)
        shared = _shared_thread_messages(service, scope, args.thread_id)
        add, remove = (["SPAM"], ["INBOX"]) if spam else (["INBOX"], ["SPAM"])
        for msg in shared:
            _modify(service, msg["id"], add, remove)
        return {
            "status": "marked_spam" if spam else "unmarked_spam",
            "thread_id": args.thread_id,
            "messages_changed": len(shared),
        }

    return handler


# -- label CRUD ------------------------------------------------------------------------------


def _validate_visibility(label_list: str | None, message_list: str | None) -> dict:
    body: dict[str, Any] = {}
    if label_list is not None:
        if label_list not in _LABEL_VISIBILITY:
            raise ToolError(
                "invalid_input", f"label_list_visibility must be one of {_LABEL_VISIBILITY}"
            )
        body["labelListVisibility"] = label_list
    if message_list is not None:
        if message_list not in _MESSAGE_VISIBILITY:
            raise ToolError("invalid_input", "message_list_visibility must be 'show' or 'hide'")
        body["messageListVisibility"] = message_list
    return body


def _sharing_names(service, scope: Scope) -> set[str]:
    by_id = {label["id"]: str(label.get("name", "")).lower() for label in _labels(service)}
    return {by_id[i] for i in _protected(scope) if i in by_id}


def create_label(args: CreateLabelInput, ctx: CallContext, session: Session) -> dict:
    service, _conn, scope = _service(session, ctx, scopes=(GMAIL_MODIFY_SCOPE, GMAIL_LABEL_SCOPE))
    if args.name.strip().lower() in _sharing_names(service, scope):
        raise ToolError(
            "forbidden", "that label controls what you share; only the user can change it"
        )
    body = {
        "name": args.name.strip(),
        **_validate_visibility(args.label_list_visibility, args.message_list_visibility),
    }
    label = service.users().labels().create(userId="me", body=body).execute()
    return {"status": "label_created", "label_id": label.get("id"), "name": label.get("name")}


def update_label(args: UpdateLabelInput, ctx: CallContext, session: Session) -> dict:
    service, _conn, scope = _service(session, ctx, scopes=(GMAIL_MODIFY_SCOPE, GMAIL_LABEL_SCOPE))
    _check_user_label(service, scope, args.label_id)
    body = _validate_visibility(args.label_list_visibility, args.message_list_visibility)
    if args.name is not None:
        if args.name.strip().lower() in _sharing_names(service, scope):
            raise ToolError("forbidden", "that name is reserved for what you share")
        body["name"] = args.name.strip()
    if not body:
        raise ToolError("invalid_input", "nothing to update")
    label = service.users().labels().patch(userId="me", id=args.label_id, body=body).execute()
    return {"status": "label_updated", "label_id": args.label_id, "name": label.get("name")}


def delete_label(args: DeleteLabelInput, ctx: CallContext, session: Session) -> dict:
    service, _conn, scope = _service(session, ctx, scopes=(GMAIL_MODIFY_SCOPE, GMAIL_LABEL_SCOPE))
    _check_user_label(service, scope, args.label_id)
    service.users().labels().delete(userId="me", id=args.label_id).execute()
    return {"status": "label_deleted", "label_id": args.label_id}


# -- sensitive label -------------------------------------------------------------------------


def _sensitive_label_id(service, scope: Scope) -> str:
    name = get_settings().gmail_sensitive_label.strip()
    if name.lower() in _sharing_names(service, scope):
        raise ToolError(
            "internal_error", "GMAIL_SENSITIVE_LABEL must differ from the sharing label"
        )
    for label in _labels(service):
        if str(label.get("name", "")).lower() == name.lower():
            return label["id"]
    created = service.users().labels().create(userId="me", body={"name": name}).execute()
    return created["id"]


def apply_sensitive_message_label(args: MessageInput, ctx: CallContext, session: Session) -> dict:
    service, _conn, scope = _service(session, ctx)
    _shared_message(service, scope, args.message_id)
    label_id = _sensitive_label_id(service, scope)
    _modify(service, args.message_id, [label_id], [])
    return {
        "status": "sensitive_label_applied",
        "message_id": args.message_id,
        "label_id": label_id,
    }


def apply_sensitive_thread_label(args: ThreadInput, ctx: CallContext, session: Session) -> dict:
    service, _conn, scope = _service(session, ctx)
    shared = _shared_thread_messages(service, scope, args.thread_id)
    label_id = _sensitive_label_id(service, scope)
    for msg in shared:
        _modify(service, msg["id"], [label_id], [])
    return {
        "status": "sensitive_label_applied",
        "thread_id": args.thread_id,
        "label_id": label_id,
        "messages_changed": len(shared),
    }


# -- registration ----------------------------------------------------------------------------

_SHARED_NOTE = " Only works on mail the user shares; unshared mail is reported as not found."


def _spec(name, input_model, handler, description, preview, *, low: bool) -> ToolSpec:
    return ToolSpec(
        name=f"google_gmail_{name}",
        provider=PROVIDER,
        risk=RiskLevel.MUTATING,
        requires_confirmation=False if low else None,
        description=description,
        input_model=input_model,
        handler=handler,
        preview_builder=preview,
    )


def register(registry) -> None:
    """Register the label/trash/spam tools. None of them can send mail."""
    specs = [
        _spec(
            "create_label",
            CreateLabelInput,
            create_label,
            "Create a Gmail label.",
            lambda a: f"Create the Gmail label '{a.name}'.",
            low=True,
        ),
        _spec(
            "update_label",
            UpdateLabelInput,
            update_label,
            "Rename a user label or change its visibility. The sharing label cannot be changed.",
            lambda a: (
                f"Update Gmail label '{a.label_id}'"
                + (f" (rename to '{a.name}')" if a.name else "")
                + "."
            ),
            low=False,
        ),
        _spec(
            "delete_label",
            DeleteLabelInput,
            delete_label,
            "Delete a user label (removes it from all mail). The sharing label cannot be deleted.",
            lambda a: f"DELETE the Gmail label '{a.label_id}' from all mail.",
            low=False,
        ),
        _spec(
            "label_message",
            MessageLabelsInput,
            label_message,
            "Add labels to a message (list_labels for ids)." + _SHARED_NOTE,
            lambda a: f"Add labels {a.label_ids} to message '{a.message_id}'.",
            low=True,
        ),
        _spec(
            "unlabel_message",
            MessageLabelsInput,
            unlabel_message,
            "Remove labels from a message." + _SHARED_NOTE,
            lambda a: f"Remove labels {a.label_ids} from message '{a.message_id}'.",
            low=True,
        ),
        _spec(
            "update_message_labels",
            UpdateMessageLabelsInput,
            update_message_labels,
            "Add and remove labels on a message in one call." + _SHARED_NOTE,
            lambda a: (
                f"Update labels on message '{a.message_id}' "
                f"(add {a.add_label_ids}, remove {a.remove_label_ids})."
            ),
            low=True,
        ),
        _spec(
            "label_thread",
            ThreadLabelsInput,
            label_thread,
            "Add labels to the shared messages of a thread." + _SHARED_NOTE,
            lambda a: f"Add labels {a.label_ids} to thread '{a.thread_id}'.",
            low=True,
        ),
        _spec(
            "unlabel_thread",
            ThreadLabelsInput,
            unlabel_thread,
            "Remove labels from the shared messages of a thread." + _SHARED_NOTE,
            lambda a: f"Remove labels {a.label_ids} from thread '{a.thread_id}'.",
            low=True,
        ),
        _spec(
            "trash_message",
            MessageInput,
            _trash_message(False),
            "Move a message to the trash." + _SHARED_NOTE,
            lambda a: f"Move message '{a.message_id}' to the trash.",
            low=False,
        ),
        _spec(
            "untrash_message",
            MessageInput,
            _trash_message(True),
            "Restore a message from the trash." + _SHARED_NOTE,
            lambda a: f"Restore message '{a.message_id}' from the trash.",
            low=True,
        ),
        _spec(
            "trash_thread",
            ThreadInput,
            _trash_thread(False),
            "Move the shared messages of a thread to the trash." + _SHARED_NOTE,
            lambda a: f"Move thread '{a.thread_id}' to the trash.",
            low=False,
        ),
        _spec(
            "untrash_thread",
            ThreadInput,
            _trash_thread(True),
            "Restore the shared messages of a thread from the trash." + _SHARED_NOTE,
            lambda a: f"Restore thread '{a.thread_id}' from the trash.",
            low=True,
        ),
        _spec(
            "mark_message_spam",
            MessageInput,
            _spam_message(True),
            "Report a message as spam (moves it out of the inbox)." + _SHARED_NOTE,
            lambda a: f"Mark message '{a.message_id}' as spam.",
            low=False,
        ),
        _spec(
            "unmark_message_spam",
            MessageInput,
            _spam_message(False),
            "Move a message out of spam back to the inbox." + _SHARED_NOTE,
            lambda a: f"Move message '{a.message_id}' out of spam.",
            low=True,
        ),
        _spec(
            "mark_thread_spam",
            ThreadInput,
            _spam_thread(True),
            "Report the shared messages of a thread as spam." + _SHARED_NOTE,
            lambda a: f"Mark thread '{a.thread_id}' as spam.",
            low=False,
        ),
        _spec(
            "unmark_thread_spam",
            ThreadInput,
            _spam_thread(False),
            "Move the shared messages of a thread out of spam." + _SHARED_NOTE,
            lambda a: f"Move thread '{a.thread_id}' out of spam.",
            low=True,
        ),
        _spec(
            "apply_sensitive_message_label",
            MessageInput,
            apply_sensitive_message_label,
            "Mark a message as sensitive by applying the configured sensitive label."
            + _SHARED_NOTE,
            lambda a: f"Apply the sensitive label to message '{a.message_id}'.",
            low=True,
        ),
        _spec(
            "apply_sensitive_thread_label",
            ThreadInput,
            apply_sensitive_thread_label,
            "Mark a thread's shared messages as sensitive with the configured label."
            + _SHARED_NOTE,
            lambda a: f"Apply the sensitive label to thread '{a.thread_id}'.",
            low=True,
        ),
    ]
    for spec in specs:
        registry.add(spec)
