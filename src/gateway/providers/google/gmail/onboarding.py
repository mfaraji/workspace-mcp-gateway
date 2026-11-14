"""First-connect Gmail setup: create the access label and tell the user how to use it.

The Gmail tools only see messages carrying the access label, so a fresh connection is
useless until that label exists. After the OAuth callback we create it (best effort) and
hand the user one short instruction. The instruction is shown once per Gmail connection:
``connection_metadata["gmail_label_onboarded"]`` is set only after the label is known to
exist, so a failed attempt is retried on the next connect.
"""

from __future__ import annotations

from typing import Any, Literal

from gateway.logging import get_logger
from gateway.oauth.google import GMAIL_LABEL_SCOPE, GMAIL_READ_SCOPE

logger = get_logger(__name__)

ONBOARDED_KEY = "gmail_label_onboarded"
LabelStatus = Literal["created", "exists", "manual"]


def needs_onboarding(scopes: list[str] | None, metadata: dict | None) -> bool:
    """True when Gmail read access was granted and the user has not been onboarded yet."""
    return GMAIL_READ_SCOPE in (scopes or []) and not (metadata or {}).get(ONBOARDED_KEY)


def ensure_access_label(service, name: str) -> Literal["created", "exists"]:
    """Create the label if it is missing. Matching is case-insensitive like Gmail's."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    if any(str(label.get("name", "")).lower() == name.lower() for label in labels):
        return "exists"
    try:
        service.users().labels().create(
            userId="me",
            body={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        ).execute()
    except Exception as exc:  # googleapiclient HttpError; 409 means a concurrent create won
        if getattr(getattr(exc, "resp", None), "status", None) == 409:
            return "exists"
        raise
    return "created"


def provision_label(service_factory, scopes: list[str] | None, name: str) -> LabelStatus:
    """Create the label when permitted; never raise (the OAuth callback must still succeed)."""
    if GMAIL_LABEL_SCOPE not in (scopes or []):
        return "manual"
    try:
        return ensure_access_label(service_factory(), name)
    except Exception:
        logger.warning("could not create Gmail access label", exc_info=True)
        return "manual"


def build_notice(status: LabelStatus, name: str) -> dict[str, Any]:
    """The one-time message shown on the connections page."""
    use = (
        f"To let the assistant read an email, open it in Gmail and apply the “{name}” label. "
        "Remove the label to stop sharing it. The assistant can create drafts but cannot send."
    )
    if status == "created":
        lead = f"Gmail is connected. We created a Gmail label called “{name}”."
    elif status == "exists":
        lead = f"Gmail is connected. Your “{name}” label is ready to use."
    else:
        lead = (
            f"Gmail is connected, but we couldn't create the label automatically. "
            f"In Gmail, create a label named “{name}”."
        )
    return {"kind": "gmail_label", "status": status, "label": name, "message": f"{lead} {use}"}
