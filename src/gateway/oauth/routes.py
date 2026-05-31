"""Google OAuth HTTP routes: start, callback, disconnect.

These routes resolve the Open WebUI user identity from headers (behind the same
trust boundary as MCP requests), run the 3-legged OAuth flow, and persist
encrypted tokens server-side. Open WebUI never receives the tokens.
"""

from __future__ import annotations

from datetime import UTC

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from gateway.config import get_settings
from gateway.db.engine import session_scope
from gateway.identity.models import IdentityError
from gateway.identity.resolver import get_or_create_user, resolve_identity
from gateway.logging import get_logger
from gateway.oauth import google as goog
from gateway.providers.google.connections import (
    StoredCredentials,
    disconnect,
    upsert_connection,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/oauth/google", tags=["oauth"])


def _resolve(request: Request):
    """Resolve the authenticated user for an OAuth route, or raise IdentityError."""
    return resolve_identity(dict(request.headers), get_settings())


def _resolve_start_user(request: Request, settings) -> str | None:
    """Resolve the external user id for the start route from identity or ticket."""
    try:
        return _resolve(request).external_user_id
    except IdentityError:
        pass
    ticket = request.query_params.get("ticket")
    if ticket:
        try:
            return goog.verify_connect_ticket(settings, ticket)
        except ValueError:
            return None
    return None


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
        return JSONResponse(
            {"error": "unauthorized", "detail": "no identity or valid connect ticket"},
            status_code=401,
        )

    state = goog.sign_state(settings, external_user_id)
    flow = goog.build_flow(settings, state=state)
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
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
        external_user_id = goog.verify_state(settings, state)
    except ValueError as exc:
        return JSONResponse({"error": "invalid_state", "detail": str(exc)}, status_code=400)

    flow = goog.build_flow(settings, state=state)
    flow.fetch_token(code=code)
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
        upsert_connection(
            session,
            user=user,
            provider_account_id=account["sub"],
            provider_email=account.get("email"),
            scopes=list(creds.scopes or goog.DEFAULT_SCOPES),
            creds=stored,
        )

    logger.info("google account connected for user %s", external_user_id)
    return RedirectResponse(f"{settings.base_url.rstrip('/')}/?connected=google", status_code=302)


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
