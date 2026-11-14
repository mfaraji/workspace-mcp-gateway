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
    AccountConflict,
    StoredCredentials,
    disconnect,
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
        include_granted_scopes="true",
        # Reauthorization must let the user select their existing connected
        # account. Otherwise Google can silently reuse a different browser
        # session and trip the V1 single-account conflict guard.
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

    with session_scope() as session:
        from gateway.identity.models import AuthenticatedUser

        user = get_or_create_user(
            session,
            AuthenticatedUser(
                external_user_id=external_user_id,
                email=account.get("email"),
            ),
        )
        try:
            conn = upsert_connection(
                session,
                user=user,
                provider_account_id=account["sub"],
                provider_email=account.get("email"),
                scopes=list(creds.scopes or goog.DEFAULT_SCOPES),
                creds=stored,
            )
        except AccountConflict as exc:
            return JSONResponse(
                {"error": "account_conflict", "detail": str(exc)}, status_code=409
            )
        connection_id = conn.id
        granted_scopes = list(conn.scopes or [])
        onboard_gmail = gmail_onboarding.needs_onboarding(granted_scopes, conn.connection_metadata)

    logger.info("google account connected for user %s", external_user_id)
    if onboard_gmail:
        _onboard_gmail(request, settings, creds, connection_id, granted_scopes)
    return RedirectResponse(f"{settings.base_url.rstrip('/')}/?connected=google", status_code=302)


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


@router.post("/disconnect")
async def disconnect_route(request: Request):
    """Revoke the calling user's Google connection."""
    try:
        auth = _resolve(request)
    except IdentityError as exc:
        return JSONResponse({"error": "unauthorized", "detail": str(exc)}, status_code=401)

    with session_scope() as session:
        from gateway.identity.models import AuthenticatedUser

        user = get_or_create_user(
            session, AuthenticatedUser(external_user_id=auth.external_user_id)
        )
        removed = disconnect(session, user.id)

    return JSONResponse({"disconnected": removed})


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
