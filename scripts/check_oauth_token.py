"""Check a Clerk access token from stdin without printing or persisting it."""

from __future__ import annotations

import argparse
import sys

from gateway.config import get_settings
from gateway.identity.models import IdentityError
from gateway.oauth.clerk import verify_access_token


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource", required=True, help="Exact HTTPS MCP resource URL")
    args = parser.parse_args()
    token = sys.stdin.read(16385).strip()
    try:
        verify_access_token(token, get_settings(), args.resource)
    except IdentityError as exc:
        print(f"Token rejected: {exc}", file=sys.stderr)
        return 1
    print("Token valid for this MCP resource with mcp:tools scope.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
