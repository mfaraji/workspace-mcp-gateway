"""The /mcp/google bundle: same tools, grants and authorization as the member connectors."""

from __future__ import annotations

from gateway.access.routes import catalog_for_resource
from gateway.connectors.registry import (
    BUNDLES,
    CONNECTORS,
    bundle_by_slug,
    bundle_members,
    by_slug,
    connector_slugs_for,
)
from gateway.mcp.server import build_mcp, product_tool_filter
from tests.test_clerk_oauth import _settings

GOOGLE = {"calendar", "drive", "tasks", "gmail"}


def test_google_bundle_contains_every_google_connector_and_nothing_else():
    bundle = bundle_by_slug("google")
    assert {c.slug for c in bundle_members(bundle)} == GOOGLE
    assert connector_slugs_for("google") == GOOGLE
    assert connector_slugs_for("gmail") == {"gmail"}
    assert connector_slugs_for("nope") is None
    # A bundle never shadows a connector's own endpoint.
    assert not {b.slug for b in BUNDLES} & {c.slug for c in CONNECTORS}
    assert by_slug("google") is None


def _tool_names(product: str | None) -> set[str]:
    settings = _settings()
    mcp = build_mcp(settings, product_tool_filter(product) if product else None)
    return {t.name for t in mcp.gateway_specs if mcp.gateway_filter(t)}


def test_bundle_endpoint_is_the_union_of_the_google_endpoints():
    union = set().union(*(_tool_names(slug) for slug in GOOGLE))
    assert _tool_names("google") == union
    assert any(n.startswith("google_gmail_") for n in union)
    assert any(n.startswith("google_calendar_") for n in union)
    # Nothing outside Google leaks into the bundle.
    bundle = _tool_names("google")
    assert not any(n.startswith(("telegram_", "apex_", "finance_", "recipes_")) for n in bundle)
    # Separate endpoints keep exposing only their own tools.
    assert not any(n.startswith("google_gmail_") for n in _tool_names("calendar"))


def test_bundle_has_no_send_tool():
    assert not any("send" in n for n in _tool_names("google") if n.startswith("google_gmail_"))


def test_permission_screens_scope_a_bundle_resource_to_its_members():
    catalog = [{"slug": c.slug, "tools": []} for c in CONNECTORS]
    assert {c["slug"] for c in catalog_for_resource(catalog, "google")} == GOOGLE
    assert [c["slug"] for c in catalog_for_resource(catalog, "gmail")] == ["gmail"]
    assert len(catalog_for_resource(catalog, None)) == len(CONNECTORS)


def test_resource_connector_accepts_bundle_resource(monkeypatch):
    from gateway.access import routes

    monkeypatch.setattr(routes, "get_settings", _settings)
    root = f"{_settings().base_url.rstrip('/')}/mcp"
    assert routes.resource_connector(f"{root}/google") == "google"
    assert routes.resource_connector(f"{root}/gmail") == "gmail"
    assert routes.resource_connector(root) is None
    try:
        routes.resource_connector(f"{root}/unknown")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown resource must be rejected")


def test_app_mounts_the_bundle_and_publishes_its_oauth_resource(monkeypatch):
    from fastapi.testclient import TestClient

    from gateway.app import create_app
    from gateway.oauth import clerk_routes

    monkeypatch.setattr(clerk_routes, "get_settings", _settings)
    app = create_app()
    mounts = {getattr(r, "path", None) for r in app.routes}
    assert "/mcp/google" in mounts and "/mcp/gmail" in mounts
    client = TestClient(app, follow_redirects=False)
    assert client.get("/mcp/google").status_code == 307  # trailing-slash redirect, like others
    meta = client.get("/.well-known/oauth-protected-resource/mcp/google")
    assert meta.status_code == 200
    assert meta.json()["resource"].endswith("/mcp/google")
    assert client.get("/.well-known/oauth-protected-resource/mcp/nope").status_code == 404
