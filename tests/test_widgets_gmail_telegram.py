"""Gmail and Telegram widgets: tool wiring, safety invariants, and UI-keyed error texts."""

from __future__ import annotations

from pathlib import Path

from gateway.mcp.server import build_mcp, product_tool_filter
from gateway.providers.google.gmail.common import with_text_fallback
from tests.test_clerk_oauth import _settings

SRC = Path(__file__).parents[1] / "src" / "gateway" / "providers"
GMAIL_UI = "ui://gmail/mail"
TELEGRAM_UI = "ui://telegram/send"


def _tools(product):
    mcp = build_mcp(_settings(), product_tool_filter(product))
    return {t.name: t for t in mcp._tool_manager.list_tools()}


def test_gmail_tools_render_and_are_callable_by_the_widget():
    tools = _tools("gmail")
    rendering = {
        "google_gmail_search_messages",
        "google_gmail_get_message",
        "google_gmail_get_thread",
        "google_gmail_list_drafts",
        "google_gmail_create_draft",
        "google_gmail_update_draft",
    }
    for name in rendering:
        assert tools[name].meta["ui"]["resourceUri"] == GMAIL_UI, name
        assert tools[name].meta["ui"]["autoApprove"] is True, name
        assert "do not" in tools[name].description and "repeat" in tools[name].description, name
    # Callable from the widget, nothing of its own to render.
    assert "resourceUri" not in tools["google_gmail_delete_draft"].meta["ui"]
    assert tools["google_gmail_delete_draft"].meta["ui"]["autoApprove"] is True


def test_gmail_still_cannot_send_and_drafts_stay_unconfirmed():
    tools = _tools("gmail")
    assert not [name for name in tools if "send" in name]
    for name in (
        "google_gmail_create_draft",
        "google_gmail_update_draft",
        "google_gmail_delete_draft",
    ):
        assert "confirmation_token" not in tools[name].parameters["properties"]


def test_html_only_messages_get_a_plain_text_body_for_the_reader():
    parsed = {"body_text": "", "body_html": "<p>Hi <b>there</b></p><script>x()</script>"}
    out = with_text_fallback(parsed)
    assert out["body_text"] == "Hi there" and out["text_from_html"] is True
    both = with_text_fallback({"body_text": "plain", "body_html": "<p>x</p>"})
    assert both["body_text"] == "plain"
    assert "text_from_html" not in with_text_fallback({"body_text": "", "body_html": ""})


def test_telegram_composer_wiring():
    tools = _tools("telegram")
    meta = tools["telegram_list_destinations"].meta["ui"]
    assert meta["resourceUri"] == TELEGRAM_UI and meta["autoApprove"] is True
    send = tools["telegram_send_message"]
    assert send.meta["ui"]["autoApprove"] is True
    assert "resourceUri" not in send.meta["ui"]
    # The gateway does not ask for a token; the widget's own "Send to ...?" step is the guard.
    assert "confirmation_token" not in send.parameters["properties"]


def test_telegram_widget_error_texts_match_the_gateway():
    """The widget tells 'delivery unconfirmed' from 'not sent' by these message fragments."""
    client = (SRC / "telegram" / "client.py").read_text()
    write = (SRC / "telegram" / "write.py").read_text()
    assert "did not confirm whether the request was delivered" in client
    assert "was already attempted" in write
    assert "already running" in write
    html = (Path(__file__).parents[1] / "src/gateway/mcp/ui/widgets/telegram.html").read_text()
    assert "did not confirm|already attempted|already running" in html
