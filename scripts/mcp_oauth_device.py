"""Acquire a Clerk OAuth token for a headless MCP client using device code.

Requires a public Clerk OAuth application with Device Authorization Grant and
the mcp:tools scope enabled. The token is written to stdout only; prompts and
status go to stderr so callers may pipe it to a secure credential store.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.parse import urlparse

import httpx


def _same_origin(url: str, issuer: str) -> bool:
    parsed, base = urlparse(url), urlparse(issuer)
    return parsed.scheme == "https" and parsed.netloc == base.netloc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issuer", required=True, help="Clerk HTTPS Frontend API URL")
    parser.add_argument("--client-id", required=True, help="Public device OAuth app client ID")
    parser.add_argument("--resource", required=True, help="Exact MCP URL, e.g. https://host/mcp")
    parser.add_argument(
        "--offline-access",
        action="store_true",
        help="Request a refresh token for a secure CLI store",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Write token response JSON to stdout for a secure CLI store",
    )
    args = parser.parse_args()
    issuer = args.issuer.rstrip("/")
    if not _same_origin(issuer, issuer) or urlparse(args.resource).scheme != "https":
        parser.error("issuer and resource must be HTTPS URLs")

    # Never store cookies: Clerk development instances treat any cookie-bearing
    # request (e.g. Cloudflare's __cf_bm from the metadata fetch) as a browser
    # and reject it with "Browser unauthenticated".
    no_cookies = CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
    try:
        with httpx.Client(timeout=10, follow_redirects=False, cookies=no_cookies) as client:
            metadata = client.get(f"{issuer}/.well-known/oauth-authorization-server")
            metadata.raise_for_status()
            metadata = metadata.json()
            if metadata.get("issuer", "").rstrip("/") != issuer:
                raise ValueError("OAuth issuer metadata mismatch")
            device_url = metadata.get("device_authorization_endpoint", "")
            token_url = metadata.get("token_endpoint", "")
            if not _same_origin(device_url, issuer) or not _same_origin(token_url, issuer):
                raise ValueError("OAuth endpoints are not on issuer origin")
            if "urn:ietf:params:oauth:grant-type:device_code" not in metadata.get(
                "grant_types_supported", []
            ):
                raise ValueError("Device Authorization Grant is not enabled in Clerk")
            response = client.post(
                device_url,
                data={
                    "client_id": args.client_id,
                    "scope": "openid mcp:tools"
                    + (" offline_access" if args.offline_access else ""),
                    "resource": args.resource,
                },
            )
            response.raise_for_status()
            device = response.json()
            print(
                f"Open {device['verification_uri']} and enter code {device['user_code']}",
                file=sys.stderr,
            )
            interval = max(1, int(device.get("interval", 5)))
            deadline = time.monotonic() + int(device["expires_in"])
            while time.monotonic() < deadline:
                time.sleep(interval)
                response = client.post(
                    token_url,
                    data={
                        "client_id": args.client_id,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": device["device_code"],
                        "resource": args.resource,
                    },
                )
                payload = response.json()
                if response.is_success:
                    if args.json:
                        print(
                            json.dumps(
                                {
                                    key: payload[key]
                                    for key in (
                                        "access_token",
                                        "refresh_token",
                                        "expires_in",
                                        "token_type",
                                    )
                                    if key in payload
                                }
                            )
                        )
                    else:
                        print(payload["access_token"])
                    return 0
                error = payload.get("error")
                if error == "authorization_pending":
                    continue
                if error == "slow_down":
                    interval += 5
                    continue
                raise ValueError(f"Device authorization failed: {error or 'unknown_error'}")
            raise ValueError("Device authorization expired")
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
