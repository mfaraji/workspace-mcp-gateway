"""Shared Gmail helpers: label scoping, message parsing, and MIME building."""

from __future__ import annotations

import base64
import re
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any

from sqlalchemy.orm import Session

from gateway.config import get_settings
from gateway.connectors.upstream import OAuthStrategy
from gateway.oauth.google import GMAIL_TOOL_SCOPES
from gateway.providers.base import CallContext
from gateway.providers.google.client import build_gmail_service
from gateway.providers.registry import ToolError

PROVIDER = "google"
MAX_BODY_CHARS = 100_000
MAX_RECIPIENTS = 50

GMAIL_OAUTH = OAuthStrategy(
    upstream_provider=PROVIDER,
    required_scopes=GMAIL_TOOL_SCOPES,
    display_name="Gmail",
    product="gmail",
)

_ADDRESS_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")
_HEADERS = ("From", "To", "Cc", "Date", "Subject", "Message-ID", "References")


def gmail_service(session: Session, ctx: CallContext):
    """Resolve the caller's active Google connection and build a Gmail service."""
    creds = GMAIL_OAUTH.get_credentials(session, ctx, get_settings())
    return build_gmail_service(creds)


def resolve_access_label_id(service) -> str:
    """Return the id of the configured access label, or raise ``label_not_found``."""
    name = get_settings().gmail_access_label
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if str(label.get("name", "")).lower() == name.lower():
            return label["id"]
    raise ToolError(
        "label_not_found",
        f"no Gmail label named '{name}'; create it and apply it to the messages "
        "the agent may access.",
    )


def is_labeled(msg: dict, label_id: str) -> bool:
    return label_id in (msg.get("labelIds") or [])


def require_labeled(msg: dict, label_id: str) -> None:
    """Treat unlabelled messages exactly like missing ones (no existence leak)."""
    if not is_labeled(msg, label_id):
        raise ToolError("not_found", "message not found")


def valid_address(value: str) -> str:
    value = value.strip()
    if not _ADDRESS_RE.match(value) or "\n" in value or "\r" in value:
        raise ValueError(f"invalid email address: {value!r}")
    return value


def _headers(payload: dict) -> dict[str, str]:
    wanted = {h.lower(): h for h in _HEADERS}
    out: dict[str, str] = {}
    for h in payload.get("headers", []):
        key = wanted.get(str(h.get("name", "")).lower())
        if key:
            out[key] = h.get("value", "")
    return out


def _decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _walk(part: dict, acc: dict[str, Any]) -> None:
    mime = part.get("mimeType", "")
    body = part.get("body", {}) or {}
    filename = part.get("filename") or ""
    if filename:
        acc["attachments"].append(
            {"filename": filename, "mime_type": mime, "size": body.get("size")}
        )
    elif mime in ("text/plain", "text/html") and body.get("data"):
        key = "text" if mime == "text/plain" else "html"
        acc[key].append(_decode(body["data"]))
    for child in part.get("parts", []) or []:
        _walk(child, acc)


def parse_message(msg: dict) -> dict[str, Any]:
    """Compact a Gmail ``format=full`` message; attachments are metadata only."""
    payload = msg.get("payload", {}) or {}
    acc: dict[str, Any] = {"text": [], "html": [], "attachments": []}
    _walk(payload, acc)
    text = "\n".join(acc["text"])
    html = "\n".join(acc["html"])
    truncated = len(text) > MAX_BODY_CHARS or len(html) > MAX_BODY_CHARS
    return {
        "id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "label_ids": msg.get("labelIds", []),
        "snippet": msg.get("snippet"),
        "headers": _headers(payload),
        "body_text": text[:MAX_BODY_CHARS],
        "body_html": html[:MAX_BODY_CHARS],
        "truncated": truncated,
        "attachments": acc["attachments"],
    }


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()


def build_mime(
    *,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    subject: str,
    body_text: str | None,
    body_html: str | None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> str:
    """Build a base64url RFC 2822 message; HTML becomes multipart/alternative."""
    msg = EmailMessage()
    if to:
        msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject.replace("\r", " ").replace("\n", " ")
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to
    if body_html:
        msg.set_content(body_text if body_text else html_to_text(body_html))
        msg.add_alternative(body_html, subtype="html")
    else:
        msg.set_content(body_text or "")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
