"""Gateway-minted Apex tokens, workspace permissions and per-call credential checks."""

from __future__ import annotations

import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import SecretStr

from gateway.access import policy
from gateway.config import Settings
from gateway.identity.models import AuthenticatedUser
from gateway.oauth import downstream
from gateway.providers.apex import client as apex_client
from gateway.providers.apex import read, wealth
from gateway.providers.base import CallContext
from gateway.providers.registry import ToolError, ToolRegistry

_KEY = ec.generate_private_key(ec.SECP256R1())
_PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()

BUSINESS = {
    "id": "w-biz",
    "name": "Acme",
    "kind": "BUSINESS",
    "capabilities": ["invoicing", "clients"],
}
PERSONAL = {
    "id": "w-main",
    "name": "Main",
    "kind": "PERSONAL",
    "capabilities": ["investments", "assets"],
}


def _settings(**overrides) -> Settings:
    base = dict(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
        base_url="https://gw.example",
        google_client_id="cid",
        google_client_secret="secret",
        apex_base_url="http://apex.local",
        gateway_signing_key=SecretStr(_PEM.replace("\n", "\\n")),
        apex_mcp_audience="apex-mcp-api",
        token_encryption_key="x" * 43 + "=",
        gateway_shared_secret="shared-secret-value",
        trusted_open_webui_origin="https://openwebui.internal",
        session_secret="sess",
    )
    base.update(overrides)
    return Settings(**base)


def _ctx(subject="user_1", client_id="client-1") -> CallContext:
    return CallContext(
        user_id=uuid.uuid4(),
        external_user_id="oauth:abc",
        request_id="req-1",
        clerk_subject=subject,
        oauth_client_id=client_id,
    )


@pytest.fixture(autouse=True)
def _clean_cache():
    apex_client.clear_workspace_cache()
    yield
    apex_client.clear_workspace_cache()


@pytest.fixture
def apex_http(monkeypatch):
    """Replace the HTTP layer: ``responses['/api/workspaces']`` is a list or a ToolError."""
    state = {"calls": [], "workspaces": [BUSINESS, PERSONAL]}

    def fake_request(settings, token, method, path, **kwargs):
        state["calls"].append((token, method, path))
        result = state["workspaces"]
        if isinstance(result, ToolError):
            raise result
        return result

    monkeypatch.setattr(apex_client, "apex_request", fake_request)
    return state


# --- minted tokens ------------------------------------------------------------


def test_minted_token_is_apex_only_and_verifies_against_the_jwks():
    settings = _settings()
    token, jti = downstream.mint_apex_token(
        settings,
        subject="user_1",
        client_id="client-1",
        workspace_id="w-biz",
        scopes=["apex:clients.read"],
    )
    jwk = downstream.jwks(settings)["keys"][0]
    claims = jwt.decode(
        token,
        jwt.PyJWK(jwk).key,
        algorithms=[jwk["alg"]],
        audience="apex-mcp-api",
        issuer="https://gw.example",
    )
    assert claims["sub"] == "user_1"
    assert claims["scope"] == "apex:clients.read"
    assert claims["wid"] == "w-biz"
    assert claims["jti"] == jti
    assert claims["act"] == {"sub": "https://gw.example", "client_id": "client-1"}
    assert claims["exp"] - claims["iat"] == settings.apex_token_ttl_seconds <= 300
    assert jwt.get_unverified_header(token)["kid"] == jwk["kid"]


def test_jwks_never_contains_private_material():
    jwk = downstream.jwks(_settings())["keys"][0]
    assert jwk["kty"] == "EC" and jwk["use"] == "sig"
    assert not {"d", "p", "q", "dp", "dq", "qi"} & set(jwk)


def test_token_is_not_valid_for_another_audience():
    settings = _settings()
    token, _ = downstream.mint_apex_token(
        settings, subject="u", client_id=None, workspace_id=None, scopes=["apex:workspaces.read"]
    )
    jwk = downstream.jwks(settings)["keys"][0]
    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(token, jwt.PyJWK(jwk).key, algorithms=[jwk["alg"]], audience="apex-ui")


def test_mint_requires_a_subject_and_scopes():
    settings = _settings()
    with pytest.raises(downstream.SigningKeyError):
        downstream.mint_apex_token(
            settings, subject="", client_id=None, workspace_id=None, scopes=["s"]
        )
    with pytest.raises(downstream.SigningKeyError):
        downstream.mint_apex_token(
            settings, subject="u", client_id=None, workspace_id=None, scopes=[]
        )


def test_missing_signing_key_is_a_clear_error():
    settings = _settings(gateway_signing_key=None)
    with pytest.raises(downstream.SigningKeyError):
        downstream.jwks(settings)
    with pytest.raises(ToolError) as exc_info:
        apex_client._mint(settings, "u", None, ["apex:workspaces.read"], None)
    assert exc_info.value.error_code == "provider_error"
    assert "GATEWAY_SIGNING_KEY" not in str(exc_info.value)


# --- user workspaces ----------------------------------------------------------


def test_user_workspaces_are_cached_per_subject(apex_http):
    settings = _settings()
    first = apex_client.user_workspaces(settings, "user_1", "client-1")
    second = apex_client.user_workspaces(settings, "user_1", "client-1")
    assert first == second and [w["id"] for w in first] == ["w-biz", "w-main"]
    assert len(apex_http["calls"]) == 1
    assert apex_http["calls"][0][1:] == ("GET", "/api/workspaces")
    apex_client.user_workspaces(settings, "user_2", "client-1")
    assert len(apex_http["calls"]) == 2


def test_unknown_apex_user_is_none_and_cached(apex_http):
    apex_http["workspaces"] = ToolError("unauthorized", "unknown subject")
    settings = _settings()
    assert apex_client.user_workspaces(settings, "ghost", None) is None
    assert apex_client.user_workspaces(settings, "ghost", None) is None
    assert len(apex_http["calls"]) == 1


def test_apex_outage_is_none_but_not_cached(apex_http):
    apex_http["workspaces"] = ToolError("provider_error", "boom")
    settings = _settings()
    assert apex_client.user_workspaces(settings, "user_1", None) is None
    apex_http["workspaces"] = [BUSINESS]
    assert apex_client.user_workspaces(settings, "user_1", None) == [BUSINESS]


# --- per-call credentials -----------------------------------------------------


def test_credentials_pin_the_workspace_and_scope(apex_http):
    settings = _settings()
    creds = apex_client.apex_credentials(
        settings,
        _ctx(),
        scopes=[apex_client.SCOPE_CLIENTS_READ],
        workspace_id="w-biz",
        capability="clients",
    )
    assert creds.headers == {"X-Workspace-Id": "w-biz"}
    claims = jwt.decode(creds.token, options={"verify_signature": False})
    assert claims["wid"] == "w-biz" and claims["scope"] == "apex:clients.read"
    assert claims["sub"] == "user_1"
    assert "token" not in repr(creds)


def test_unknown_workspace_is_invalid_input(apex_http):
    with pytest.raises(ToolError) as exc_info:
        apex_client.apex_credentials(
            _settings(), _ctx(), scopes=["s"], workspace_id="nope", capability="clients"
        )
    assert exc_info.value.error_code == "invalid_input"


def test_workspace_without_the_capability_is_forbidden_without_an_apex_call(apex_http):
    with pytest.raises(ToolError) as exc_info:
        apex_client.apex_credentials(
            _settings(), _ctx(), scopes=["s"], workspace_id="w-main", capability="clients"
        )
    assert exc_info.value.error_code == "forbidden"
    # Only the workspace listing happened; no business call was attempted.
    assert [c[2] for c in apex_http["calls"]] == ["/api/workspaces"]


def test_unrecognised_apex_user_is_forbidden(apex_http):
    apex_http["workspaces"] = ToolError("unauthorized", "unknown subject")
    with pytest.raises(ToolError) as exc_info:
        apex_client.apex_credentials(
            _settings(), _ctx(), scopes=["s"], workspace_id="w-biz", capability="clients"
        )
    assert exc_info.value.error_code == "forbidden"


def test_wealth_workspace_capability_means_personal_kind():
    assert apex_client.workspace_has(PERSONAL, apex_client.CAP_WEALTH_WORKSPACE)
    assert not apex_client.workspace_has(BUSINESS, apex_client.CAP_WEALTH_WORKSPACE)
    assert apex_client.workspace_has(BUSINESS, None)


# --- which tools Apex permits -------------------------------------------------


def _specs():
    registry = ToolRegistry()
    read.register(registry)
    wealth.register(registry)
    return {s.name: s for s in registry._specs}


def test_each_apex_tool_declares_its_capability():
    specs = _specs()
    assert specs["apex_list_clients"].required_capability == "clients"
    assert specs["apex_list_timesheets"].required_capability == "invoicing"
    assert specs["apex_wealth_get_positions"].required_capability == "investments"


def test_permits_follows_workspace_capabilities(apex_http):
    settings = _settings()
    specs = _specs()
    upstream = apex_client.APEX_UPSTREAM
    apex_http["workspaces"] = [BUSINESS]
    assert upstream.permits(settings, "user_1", None, specs["apex_list_clients"])
    assert not upstream.permits(settings, "user_1", None, specs["apex_wealth_get_positions"])
    apex_client.clear_workspace_cache()
    apex_http["workspaces"] = [PERSONAL]
    assert not upstream.permits(settings, "user_1", None, specs["apex_list_clients"])
    assert upstream.permits(settings, "user_1", None, specs["apex_wealth_get_positions"])


def test_nothing_is_permitted_for_an_unknown_apex_user(apex_http):
    apex_http["workspaces"] = ToolError("unauthorized", "unknown subject")
    specs = _specs()
    assert not apex_client.APEX_UPSTREAM.permits(
        _settings(), "ghost", None, specs["apex_list_clients"]
    )
    assert not apex_client.APEX_UPSTREAM.permits(
        _settings(), None, None, specs["apex_list_clients"]
    )


def test_policy_consults_the_connector_upstream(apex_http):
    settings = _settings()
    specs = _specs()
    auth = AuthenticatedUser(
        external_user_id="oauth:abc",
        source="oauth",
        clerk_subject="user_1",
        oauth_client_id="client-1",
    )
    apex_http["workspaces"] = [BUSINESS]
    assert policy.upstream_permits(settings, auth, specs["apex_list_clients"])
    assert not policy.upstream_permits(settings, auth, specs["apex_wealth_get_positions"])


def test_catalog_hides_tools_apex_does_not_permit(apex_http):
    settings = _settings()
    specs = _specs()
    apex_http["workspaces"] = [BUSINESS]
    catalog = [
        {
            "slug": "apex",
            "access_mode": "personal",
            "tools": [{"name": "apex_list_clients"}, {"name": "apex_list_timesheets"}],
        },
        {
            "slug": "apex-wealth",
            "access_mode": "personal",
            "tools": [{"name": "apex_wealth_get_positions"}],
        },
    ]
    filtered = policy.permitted_catalog(settings, "user_1", catalog, specs)
    names = {t["name"] for c in filtered for t in c["tools"]}
    assert names == {"apex_list_clients", "apex_list_timesheets"}
