# AGENTS.md

Guidance for coding agents working in this repository. Humans may read it too.

## What this is

`workspace-mcp-gateway` is a multi-user MCP gateway for Open WebUI. Each Open
WebUI user authorizes their own Google Workspace account; the gateway stores
encrypted per-user tokens server-side and exposes one Streamable HTTP MCP
endpoint. Provider integrations are modular so new providers can be added without
reworking the core.

The authoritative product/scope document is [`spec.md`](./spec.md) — read it
before making design decisions. It defines V1 milestones and acceptance criteria.

## Layout

```
src/gateway/
  app.py            ASGI app factory (create_app)
  config.py         Settings (pydantic-settings, reads .env); get_settings() cached
  db/               engine + session_scope, ORM models, Base
  crypto/           Fernet token encryption (get_cipher)
  identity/         AuthenticatedUser, header-based identity resolution
  mcp/              FastMCP server build + IdentityMiddleware / current-user ContextVar
  oauth/            Google OAuth flow (scopes, Flow, signed state) + routes
  policy/           confirmation-token policy seam (confirm.check)
  audit/            audit-log writer + input redaction (summarize_input)
  providers/        provider-agnostic ToolSpec/registry + provider modules
    google/
      client.py         authorized Google API service builders
      connections.py    connection/token persistence + refresh
      calendar/         read.py (read tools), write.py (mutating tools), common.py
migrations/         Alembic migrations
tests/              pytest suite (some DB-backed, auto-skip without Postgres)
scripts/            local dev helpers (setup_local_db.sh)
```

## Setup & common commands

Dev tooling runs in the host virtualenv (`.venv`), **not** in the Docker image —
the image is the lean runtime (`uv sync --no-dev`, no tests, no pytest/ruff).

```bash
uv sync                                  # install deps into .venv (incl. dev)
cp .env.example .env                     # then fill in real values
sudo ./scripts/setup_local_db.sh         # provision local Postgres role + db
.venv/bin/alembic upgrade head           # apply migrations
.venv/bin/python -m pytest -q            # run tests
.venv/bin/ruff check src tests           # lint (must be clean)
```

DB-backed tests skip automatically when Postgres is unreachable, so the unit
suite still runs anywhere. The host venv and the Docker container share the same
local Postgres (host `127.0.0.1:5432`, container `host.docker.internal:5432`).

Run the server locally: `uv run uvicorn gateway.app:create_app --factory --reload`.

## Architecture conventions

- **Every tool is a `ToolSpec`** (`providers/base.py`) registered via a provider
  module's `register(registry)` entrypoint, wired in `mcp/server.py`. Adding a
  provider is a one-line change there.
- **One uniform pipeline** (`providers/registry.py`) wraps every call:
  resolve identity → validate input → policy/confirm → execute handler →
  write audit. This is the single chokepoint for identity enforcement and audit.
- **Risk levels.** `RiskLevel.READ` executes directly. `RiskLevel.MUTATING` flows
  through the confirmation gate: the first call returns a preview + short-lived
  signed `confirmation_token`; the model must re-invoke with that token. Provide
  a `preview_builder` on the spec for a human-readable confirmation message.
- **Audit must never leak secrets.** `audit/log.py` summarizes input from a
  per-tool whitelist of safe scalar fields; free-text (queries, summaries,
  descriptions) and tokens are dropped. Add a `_SAFE_FIELDS` entry for each new
  tool — default is "record nothing".
- **One audit row per call**, including failures. Success/needs-confirmation
  audits commit with the call; failures audit in a separate transaction because
  the tool's own transaction rolls back (`_audit_error`). The user is resolved in
  its own committed transaction first so the audit FK stays valid.
- **Tokens are encrypted at rest** (Fernet) and refreshed under a row lock.
  Open WebUI never receives Google tokens.
- **Tool naming** is provider-prefixed: `google_calendar_list_events`, etc.
- **Minimal OAuth scopes.** Only request scopes for tools actually registered in
  `build_mcp()` (incremental authorization). When you wire up a new provider
  module, union its scopes into `DEFAULT_SCOPES` in `oauth/google.py` at the same
  time — not before.

## Adding a tool (checklist)

1. Define a pydantic input model and a handler `(model, ctx, session) -> result`.
2. Build the authorized client via `providers/google/client.py`.
3. Register a `ToolSpec` with the right `RiskLevel` (+ `preview_builder` if mutating).
4. Add a `_SAFE_FIELDS` whitelist entry in `audit/log.py`.
5. If it needs a new Google scope, add it to `oauth/google.py` and `DEFAULT_SCOPES`.
6. Wire the module's `register()` into `mcp/server.py`.
7. Add tests; run pytest + ruff.

## House rules

- Match the surrounding style: module docstrings, type hints, `from __future__
  import annotations`, ruff line length 100.
- Keep ruff clean and tests green before considering work done.
- Never log or persist raw tokens, file contents, or free-text event fields.
- Use Conventional Commits (`feat:`, `fix:`, `chore:`, `test:`, `docs:`, ...).
