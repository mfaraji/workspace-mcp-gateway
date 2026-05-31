"""Management CLI for native-client bearer tokens.

Mint, list, and revoke the static per-user tokens that native MCP clients
(Cursor, Claude Desktop) use to authenticate to the gateway. Run with::

    python -m gateway.tokens create --user <external_user_id> --name "cursor-laptop"
    python -m gateway.tokens list --user <external_user_id>
    python -m gateway.tokens revoke <token_prefix>

The plaintext token is shown exactly once, at creation; only its SHA-256 hash is
stored. A token maps to a ``User`` row, so it reuses any Google connection that
user already made through Open WebUI.
"""

from __future__ import annotations

import argparse
import secrets
from datetime import UTC, datetime

from sqlalchemy import select

from gateway.config import get_settings
from gateway.crypto.tokens import hash_token
from gateway.db.engine import session_scope
from gateway.db.models import ClientToken, User
from gateway.identity.models import AuthenticatedUser
from gateway.identity.resolver import get_or_create_user
from gateway.oauth.google import build_start_url

TOKEN_SCHEME = "wmcp_"
PREFIX_LEN = 12


def _generate_token() -> tuple[str, str]:
    """Return ``(plaintext, prefix)`` for a fresh ~256-bit token."""
    plaintext = TOKEN_SCHEME + secrets.token_urlsafe(32)
    return plaintext, plaintext[:PREFIX_LEN]


def cmd_create(args: argparse.Namespace) -> int:
    settings = get_settings()
    plaintext, prefix = _generate_token()
    with session_scope() as session:
        user = get_or_create_user(
            session,
            AuthenticatedUser(external_user_id=args.user, email=args.email),
        )
        session.add(
            ClientToken(
                user_id=user.id,
                name=args.name,
                token_hash=hash_token(plaintext),
                token_prefix=prefix,
            )
        )
        connect_url = build_start_url(settings, args.user)

    print(f"Token created for user {args.user!r} (name={args.name!r}).")
    print("Store this token now — it will not be shown again:\n")
    print(f"    {plaintext}\n")
    print("Configure it in your MCP client as: Authorization: Bearer <token>")
    print(f"\nIf this user hasn't connected Google yet, open this link once:\n    {connect_url}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    with session_scope() as session:
        user = session.scalar(select(User).where(User.external_user_id == args.user))
        if user is None:
            print(f"No user {args.user!r}.")
            return 1
        tokens = session.scalars(
            select(ClientToken).where(ClientToken.user_id == user.id)
        ).all()
        if not tokens:
            print(f"No tokens for user {args.user!r}.")
            return 0
        for t in tokens:
            state = "revoked" if t.revoked_at else "active"
            last = t.last_used_at.isoformat() if t.last_used_at else "never"
            print(f"{t.token_prefix}…  {state:7}  name={t.name!r}  last_used={last}")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    with session_scope() as session:
        token = session.scalar(
            select(ClientToken).where(ClientToken.token_prefix == args.prefix)
        )
        if token is None:
            print(f"No token with prefix {args.prefix!r}.")
            return 1
        if token.revoked_at:
            print(f"Token {args.prefix!r} already revoked.")
            return 0
        token.revoked_at = datetime.now(UTC)
    print(f"Revoked token {args.prefix!r}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gateway.tokens", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="mint a token for a user")
    p_create.add_argument("--user", required=True, help="external (Open WebUI) user id")
    p_create.add_argument("--name", required=True, help="label, e.g. cursor-laptop")
    p_create.add_argument("--email", default=None, help="optional email for a new user")
    p_create.set_defaults(func=cmd_create)

    p_list = sub.add_parser("list", help="list a user's tokens")
    p_list.add_argument("--user", required=True, help="external (Open WebUI) user id")
    p_list.set_defaults(func=cmd_list)

    p_revoke = sub.add_parser("revoke", help="revoke a token by prefix")
    p_revoke.add_argument("prefix", help="token prefix shown by `list`")
    p_revoke.set_defaults(func=cmd_revoke)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
