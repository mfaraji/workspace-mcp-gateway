"""Device-code CLI follows Clerk metadata and polling semantics."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "mcp_oauth_device.py"
    spec = importlib.util.spec_from_file_location("mcp_oauth_device", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_device_flow_uses_public_client_and_obeys_slow_down(monkeypatch, capsys):
    cli = _load_cli()
    sleeps = []
    posts = []

    class Response:
        def __init__(self, data, success=True):
            self.data = data
            self.is_success = success

        def raise_for_status(self):
            return None

        def json(self):
            return self.data

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["cookies"]._policy.allowed_domains() == ()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url):
            assert url == "https://clerk.example/.well-known/oauth-authorization-server"
            return Response(
                {
                    "issuer": "https://clerk.example",
                    "device_authorization_endpoint": "https://clerk.example/oauth/device_authorization",
                    "token_endpoint": "https://clerk.example/oauth/token",
                    "grant_types_supported": ["urn:ietf:params:oauth:grant-type:device_code"],
                }
            )

        def post(self, url, data):
            posts.append((url, data))
            if url.endswith("device_authorization"):
                return Response(
                    {
                        "device_code": "private-code",
                        "user_code": "ABCD-EFGH",
                        "verification_uri": "https://clerk.example/device",
                        "expires_in": 600,
                        "interval": 2,
                    }
                )
            return [
                Response({"error": "authorization_pending"}, False),
                Response({"error": "slow_down"}, False),
                Response({"access_token": "test-access-token", "refresh_token": "test-refresh"}),
            ][len(posts) - 2]

    monkeypatch.setattr(cli.httpx, "Client", Client)
    monkeypatch.setattr(cli.time, "sleep", sleeps.append)
    monkeypatch.setattr(cli.time, "monotonic", lambda: 0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mcp_oauth_device.py",
            "--issuer",
            "https://clerk.example",
            "--client-id",
            "public-client",
            "--resource",
            "https://mcp.example/mcp/telegram",
        ],
    )

    assert cli.main() == 0
    output = capsys.readouterr()
    assert output.out.strip() == "test-access-token"
    assert "ABCD-EFGH" in output.err
    assert sleeps == [2, 2, 7]
    assert all("client_secret" not in data for _, data in posts)
    assert all(data["resource"] == "https://mcp.example/mcp/telegram" for _, data in posts)

    posts.clear()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mcp_oauth_device.py",
            "--issuer",
            "https://clerk.example",
            "--client-id",
            "public-client",
            "--resource",
            "https://mcp.example/mcp/telegram",
            "--offline-access",
            "--json",
        ],
    )
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["refresh_token"] == "test-refresh"
    assert "offline_access" in posts[0][1]["scope"]
