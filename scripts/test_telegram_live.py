#!/usr/bin/env python3
"""One-off live check against Telegram Bot API (reads token from .telegram_token.local)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = ROOT / ".telegram_token.local"
API = "https://api.telegram.org"


def main() -> int:
    if not TOKEN_FILE.is_file():
        print("Missing .telegram_token.local", file=sys.stderr)
        return 1
    token = TOKEN_FILE.read_text().strip()
    chat_id = sys.argv[1] if len(sys.argv) > 1 else None

    me = httpx.get(f"{API}/bot{token}/getMe", timeout=30.0).json()
    print("getMe:", json.dumps(me, indent=2))

    if not me.get("ok"):
        return 1

    updates = httpx.get(f"{API}/bot{token}/getUpdates", timeout=30.0).json()
    print("getUpdates (chat ids):", json.dumps(updates, indent=2))

    if chat_id:
        body = {"chat_id": chat_id, "text": "workspace-mcp-gateway Telegram connector test"}
        sent = httpx.post(f"{API}/bot{token}/sendMessage", json=body, timeout=30.0).json()
        print("sendMessage:", json.dumps(sent, indent=2))
        return 0 if sent.get("ok") else 1

    print("\nTo send a test message, run:")
    print("  .venv/bin/python scripts/test_telegram_live.py <chat_id>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
