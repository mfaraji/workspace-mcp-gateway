"""Google OAuth flow helpers: scopes, Flow construction, and signed state.

Scopes are organized per-product so incremental authorization can union exactly
the scopes the enabled tools require. The current build enables Calendar, Drive,
and Tasks tools, so we request their scopes plus the OpenID scopes needed to
identify the Google account.
"""

from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from gateway.config import Settings

# OpenID scopes — required to learn the Google account id (sub) and email.
OPENID_SCOPES = ["openid", "email", "profile"]

# Per-product Google scopes (extend as tools are enabled — incremental auth).
CALENDAR_READ_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
CALENDAR_WRITE_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    # drive.file: per-file access limited to files the gateway creates/opens.
    "https://www.googleapis.com/auth/drive.file",
]

TASKS_SCOPES = [
    "https://www.googleapis.com/auth/tasks.readonly",
    "https://www.googleapis.com/auth/tasks",
]

# Scopes requested by the current build. Only request scopes for products with
# tools actually registered in build_mcp(); DRIVE_SCOPES/TASKS_SCOPES are unioned
# in here as their provider modules are wired up (incremental authorization),
# keeping consent and token blast radius minimal until then.
DEFAULT_SCOPES = OPENID_SCOPES + CALENDAR_READ_SCOPES + CALENDAR_WRITE_SCOPES

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

_STATE_SALT = "google-oauth-state"
_STATE_MAX_AGE_SECONDS = 600  # 10 minutes

_CONNECT_TICKET_SALT = "google-connect-ticket"
_CONNECT_TICKET_MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 days


def _client_config(settings: Settings) -> dict:
    return {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": GOOGLE_AUTH_URI,
            "token_uri": GOOGLE_TOKEN_URI,
            "redirect_uris": [settings.google_redirect_uri],
        }
    }


def build_flow(settings: Settings, scopes: list[str] | None = None, state: str | None = None):
    """Construct a google-auth-oauthlib ``Flow`` for the web redirect dance."""
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        _client_config(settings),
        scopes=scopes or DEFAULT_SCOPES,
        state=state,
    )
    flow.redirect_uri = settings.google_redirect_uri
    return flow


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt=_STATE_SALT)


def _ticket_serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt=_CONNECT_TICKET_SALT)


def sign_connect_ticket(settings: Settings, external_user_id: str) -> str:
    """Sign a one-time link that lets a native-client user start the Google flow.

    Native clients authenticate with a bearer token, but a browser visiting
    ``/oauth/google/start`` carries no identity. The token CLI hands the user this
    signed ticket so the OAuth start route can bind the flow to them.
    """
    return _ticket_serializer(settings).dumps({"uid": external_user_id})


def verify_connect_ticket(settings: Settings, ticket: str) -> str:
    """Verify a connect ticket and return the bound external user id.

    Raises:
        ValueError: if the ticket is missing, tampered, or expired.
    """
    try:
        data = _ticket_serializer(settings).loads(
            ticket, max_age=_CONNECT_TICKET_MAX_AGE_SECONDS
        )
    except SignatureExpired as exc:
        raise ValueError("connect ticket expired") from exc
    except BadSignature as exc:
        raise ValueError("invalid connect ticket") from exc
    uid = data.get("uid")
    if not uid:
        raise ValueError("connect ticket missing user binding")
    return uid


def sign_state(settings: Settings, external_user_id: str) -> str:
    """Sign an OAuth ``state`` value binding the callback to a specific user."""
    return _serializer(settings).dumps({"uid": external_user_id})


def verify_state(settings: Settings, state: str) -> str:
    """Verify a signed state and return the bound external user id.

    Raises:
        ValueError: if the state is missing, tampered, or expired.
    """
    try:
        data = _serializer(settings).loads(state, max_age=_STATE_MAX_AGE_SECONDS)
    except SignatureExpired as exc:
        raise ValueError("oauth state expired") from exc
    except BadSignature as exc:
        raise ValueError("invalid oauth state") from exc
    uid = data.get("uid")
    if not uid:
        raise ValueError("oauth state missing user binding")
    return uid
