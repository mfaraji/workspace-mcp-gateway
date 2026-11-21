"""Google OAuth HTTP routes: start, callback, disconnect.

These routes resolve the Open WebUI user identity from headers (behind the same
trust boundary as MCP requests), run the 3-legged OAuth flow, and persist
encrypted tokens server-side. Open WebUI never receives the tokens.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from oauthlib.oauth2 import OAuth2Error

from gateway.access.routes import request_user
from gateway.config import get_settings
from gateway.db.engine import session_scope
from gateway.identity.models import IdentityError
from gateway.identity.resolver import get_or_create_user
from gateway.logging import get_logger
from gateway.oauth import google as goog
from gateway.providers.google import client as google_client
from gateway.providers.google.connections import (
    StoredCredentials,
    disconnect_connection,
    set_default,
    upsert_connection,
)
from gateway.providers.google.gmail import onboarding as gmail_onboarding

logger = get_logger(__name__)
router = APIRouter(prefix="/oauth/google", tags=["oauth"])


@contextmanager
def _relax_oauthlib_scope_check() -> Iterator[None]:
    """Allow Google's equivalent OpenID scope aliases during token exchange."""
    previous = os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE")
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("OAUTHLIB_RELAX_TOKEN_SCOPE", None)
        else:
            os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = previous


def _resolve(request: Request):
    """Resolve the authenticated user for an OAuth route, or raise IdentityError."""
    return request_user(request)


def _resolve_start_user(request: Request, settings) -> str | None:
    """Resolve the external user id for the start route from identity or ticket."""
    try:
        auth = _resolve(request)
    except IdentityError:
        return None
    ticket = request.query_params.get("ticket")
    if ticket:
        try:
            if goog.verify_connect_ticket(settings, ticket) != auth.external_user_id:
                return None
        except ValueError:
            return None
    return auth.external_user_id


@router.get("/start")
async def start(request: Request):
    """Begin the Google OAuth flow for the calling user.

    Identifies the user from the trusted Open WebUI headers, or — for native
    clients whose browser carries no identity — from a signed ``ticket`` query
    param minted by the token CLI.
    """
    settings = get_settings()

    external_user_id = _resolve_start_user(request, settings)
    if external_user_id is None:
        request.session["return_to"] = str(request.url.path) + (
            "?" + request.url.query if request.url.query else ""
        )
        return RedirectResponse("/access/login", status_code=303)
    product = request.query_params.get("product")
    try:
        scopes = goog.scopes_for_product(product)
        normalized_product = goog.normalize_product(product)
    except ValueError as exc:
        return JSONResponse({"error": "invalid_product", "detail": str(exc)}, status_code=400)

    state = goog.sign_state(settings, external_user_id, product=normalized_product)
    flow = goog.build_flow(settings, scopes=scopes, state=state)
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="false",
        # Always show the account chooser so the user can add another Google account
        # (or pick the one to reauthorize) instead of silently reusing a browser session.
        prompt="consent select_account",
    )
    return RedirectResponse(authorization_url, status_code=302)


@router.get("/callback")
async def callback(request: Request):
    """Handle the Google OAuth redirect: exchange code, store encrypted tokens."""
    settings = get_settings()

    error = request.query_params.get("error")
    if error:
        return JSONResponse({"error": "oauth_denied", "detail": error}, status_code=400)

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    try:
        state_payload = goog.verify_state_payload(settings, state)
    except ValueError as exc:
        return JSONResponse({"error": "invalid_state", "detail": str(exc)}, status_code=400)

    external_user_id = state_payload["uid"]
    product = state_payload.get("product")
    try:
        flow = goog.build_flow(settings, scopes=goog.scopes_for_product(product), state=state)
    except ValueError as exc:
        return JSONResponse({"error": "invalid_product", "detail": str(exc)}, status_code=400)
    try:
        with _relax_oauthlib_scope_check():
            flow.fetch_token(code=code, include_client_id=True)
    except OAuth2Error as exc:
        logger.warning("google oauth token exchange failed: %s", exc)
        return JSONResponse(
            {
                "error": "oauth_token_exchange_failed",
                "oauth_error": exc.error,
                "detail": exc.description,
            },
            status_code=400,
        )
    creds = flow.credentials

    account = _identify_google_account(creds, settings)

    expires_at = creds.expiry.replace(tzinfo=UTC) if creds.expiry else None
    stored = StoredCredentials(
        access_token=creds.token,
        refresh_token=creds.refresh_token,
        expires_at=expires_at,
    )

    granted = list(creds.scopes or goog.scopes_for_product(product))
    # One connection per product: a single-product start stores that product; the
    # "all" bundle stores one connection (own token copy) for each product granted.
    products = [goog.normalize_product(product)] if goog.normalize_product(product) else [
        p for p in goog.PRODUCT_SCOPES if p in goog.OAUTH_ENABLED_PRODUCTS
    ]
    onboard: tuple | None = None
    with session_scope() as session:
        from gateway.identity.models import AuthenticatedUser

        user = get_or_create_user(
            session,
            AuthenticatedUser(
                external_user_id=external_user_id,
                email=account.get("email"),
            ),
        )
        for prod in products:
            prod_scopes = set(goog.PRODUCT_SCOPES[prod])
            if not prod_scopes & set(granted):
                continue  # the user unticked this product on the consent screen
            conn = upsert_connection(
                session,
                user=user,
                provider_account_id=account["sub"],
                provider_email=account.get("email"),
                scopes=[s for s in granted if s in prod_scopes or s in goog.OPENID_SCOPES],
                creds=stored,
                product=prod,
            )
            if prod == "gmail" and gmail_onboarding.needs_onboarding(
                list(conn.scopes or []), conn.connection_metadata
            ):
                onboard = (conn.id, list(conn.scopes or []))

    logger.info("google account connected for user %s", external_user_id)
    if onboard:
        _onboard_gmail(request, settings, creds, onboard[0], onboard[1])
    suffix = f"?connected={goog.normalize_product(product) or 'google'}"
    return RedirectResponse(f"{settings.base_url.rstrip('/')}/connections{suffix}", status_code=302)


def _onboard_gmail(request: Request, settings, creds, connection_id, granted_scopes) -> None:
    """Create the Gmail access label and queue the one-time instruction for the user.

    Runs after the connection is committed, so it can never fail or delay the connect
    itself. The "already onboarded" flag is only written once the label is known to exist.
    """
    from gateway.db.models import ProviderConnection

    name = settings.gmail_access_label
    status = gmail_onboarding.provision_label(
        lambda: google_client.build_gmail_service(creds), granted_scopes, name
    )
    if status != "manual":
        with session_scope() as session:
            conn = session.get(ProviderConnection, connection_id)
            if conn is not None:
                conn.connection_metadata = {
                    **(conn.connection_metadata or {}),
                    gmail_onboarding.ONBOARDED_KEY: True,
                }
    request.session["notice"] = gmail_onboarding.build_notice(status, name)


def _connection_action(request: Request, connection_id: str, action) -> JSONResponse:
    """Run an owner-scoped, CSRF-protected change on one of the caller's connections."""
    import uuid as _uuid

    from fastapi import HTTPException

    from gateway.identity.models import AuthenticatedUser

    try:
        auth = _resolve(request)
    except IdentityError as exc:
        return JSONResponse({"error": "unauthorized", "detail": str(exc)}, status_code=401)
    try:
        conn_id = _uuid.UUID(connection_id)
    except ValueError as exc:
        raise HTTPException(404, "Connection not found") from exc
    with session_scope() as session:
        user = get_or_create_user(
            session, AuthenticatedUser(external_user_id=auth.external_user_id)
        )
        if not action(session, user.id, conn_id):
            raise HTTPException(404, "Connection not found")
    return JSONResponse({"ok": True})


@router.post("/connections/{connection_id}/disconnect")
async def disconnect_route(request: Request, connection_id: str):
    """Disconnect one connected Google account from one product."""
    return _connection_action(request, connection_id, disconnect_connection)


@router.post("/connections/{connection_id}/default")
async def default_route(request: Request, connection_id: str):
    """Make one connected account the default for its product."""
    return _connection_action(request, connection_id, set_default)


def _gmail_connection(session, user_id, connection_id):
    from gateway.db.models import ProviderConnection

    conn = session.get(ProviderConnection, connection_id)
    if (
        conn is None
        or conn.user_id != user_id
        or conn.status != "active"
        or conn.provider != "google"
        or conn.product != "gmail"
    ):
        return None
    return conn


def _gmail_labels(session, conn, settings) -> list[str]:
    """The account's user-created label names (system labels are never offered)."""
    from gateway.providers.google.connections import load_credentials

    service = google_client.build_gmail_service(load_credentials(session, conn, settings))
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    return sorted(
        (str(x["name"]) for x in labels if x.get("type") == "user" and x.get("name")),
        key=str.lower,
    )


@router.get("/connections/{connection_id}/gmail-sharing")
async def gmail_sharing_get(request: Request, connection_id: str):
    """Current sharing choice for one Gmail account plus the labels it could share."""
    return _gmail_sharing(request, connection_id, None)


@router.put("/connections/{connection_id}/gmail-sharing")
async def gmail_sharing_put(request: Request, connection_id: str):
    """Set which Gmail messages one account shares: chosen labels or all mail."""
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    return _gmail_sharing(request, connection_id, body if isinstance(body, dict) else {})


def _gmail_sharing(request: Request, connection_id: str, update: dict | None) -> JSONResponse:
    import uuid as _uuid

    from fastapi import HTTPException

    from gateway.identity.models import AuthenticatedUser
    from gateway.providers.google.gmail import sharing as gmail_sharing

    settings = get_settings()
    try:
        auth = _resolve(request)
    except IdentityError as exc:
        return JSONResponse({"error": "unauthorized", "detail": str(exc)}, status_code=401)
    try:
        conn_id = _uuid.UUID(connection_id)
    except ValueError as exc:
        raise HTTPException(404, "Connection not found") from exc
    with session_scope() as session:
        user = get_or_create_user(
            session, AuthenticatedUser(external_user_id=auth.external_user_id)
        )
        conn = _gmail_connection(session, user.id, conn_id)
        if conn is None:
            raise HTTPException(404, "Connection not found")
        try:
            available = _gmail_labels(session, conn, settings)
        except Exception:
            logger.warning("could not list Gmail labels for sharing settings", exc_info=True)
            return JSONResponse({"error": "gmail_unavailable"}, status_code=502)
        if update is not None:
            mode = update.get("mode")
            if mode == gmail_sharing.MODE_ALL and update.get("confirm") is not True:
                return JSONResponse({"error": "confirmation_required"}, status_code=400)
            labels = update.get("labels")
            try:
                chosen = gmail_sharing.normalize(
                    str(mode), labels if isinstance(labels, list) else []
                )
            except ValueError as exc:
                return JSONResponse(
                    {"error": "invalid_request", "detail": str(exc)}, status_code=400
                )
            known = {n.lower() for n in available}
            unknown = [n for n in chosen.labels if n.lower() not in known]
            if unknown:
                return JSONResponse(
                    {"error": "unknown_label", "detail": ", ".join(unknown)}, status_code=400
                )
            conn.connection_metadata = {
                **(conn.connection_metadata or {}),
                gmail_sharing.SHARING_KEY: chosen.as_dict(),
            }
        current = gmail_sharing.get_sharing(conn.connection_metadata, settings.gmail_access_label)
    return JSONResponse(
        {"sharing": current.as_dict(), "labels": available, "email": conn.provider_email}
    )


def _identify_google_account(creds, settings) -> dict:
    """Return ``{"sub", "email"}`` for the authenticated Google account.

    Verifies the OpenID id_token returned alongside the access token.
    """
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    id_tok = getattr(creds, "id_token", None)
    if id_tok:
        info = google_id_token.verify_oauth2_token(
            id_tok, google_requests.Request(), settings.google_client_id
        )
        return {"sub": info["sub"], "email": info.get("email")}

    # Fallback: call the userinfo endpoint with the access token.
    import requests

    resp = requests.get(
        "https://openidconnect.googleapis.com/v1/userinfo",
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return {"sub": data["sub"], "email": data.get("email")}
