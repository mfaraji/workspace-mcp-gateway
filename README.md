# Workspace MCP Gateway

A multi-user MCP gateway for [Open WebUI](https://github.com/open-webui/open-webui).
Each Open WebUI user connects their own Google Workspace account; the gateway
exposes Streamable HTTP MCP endpoints and keeps provider integrations modular.

This is the **V1 vertical slice**: gateway skeleton, Postgres schema, Google
OAuth with encrypted token storage, and Google Calendar, Drive, and Tasks read
tools. See
[`spec.md`](./spec.md) for the full design and roadmap.

## Architecture

```
Open WebUI -> workspace-mcp-gateway
                ├─ identity/trust-boundary layer   (src/gateway/identity)
                ├─ MCP Streamable HTTP server       (src/gateway/mcp)
                ├─ tool registry + policy layer      (src/gateway/providers, policy)
                ├─ provider modules (google/calendar) (src/gateway/providers/google)
                └─ Postgres token/audit store        (src/gateway/db)
```

The gateway owns all user-provider connection state. Open WebUI never receives
Google refresh tokens.

## Local development

### 1. Toolchain

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- A local PostgreSQL 16 server

### 2. Postgres (local server)

Create a dedicated role and database on the host Postgres:

```sql
sudo -u postgres psql <<'SQL'
CREATE ROLE workspace_mcp LOGIN PASSWORD 'wmcp_local_dev_pw';
CREATE DATABASE workspace_mcp OWNER workspace_mcp;
GRANT ALL PRIVILEGES ON DATABASE workspace_mcp TO workspace_mcp;
SQL
```

The provided Docker setup runs only the gateway app. It expects this host
Postgres database to already exist.

### 3. Environment

```bash
cp .env.example .env
# Generate secrets:
python -c "from cryptography.fernet import Fernet; print('TOKEN_ENCRYPTION_KEY=' + Fernet.generate_key().decode())"
python -c "import secrets; print('SESSION_SECRET=' + secrets.token_urlsafe(48))"
# Fill in GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET from a Google Cloud OAuth
# client (Web application) whose redirect URI is:
#   http://localhost:8000/oauth/google/callback
```

For local testing without a reverse proxy, set `DEV_TRUST_ALL_ORIGINS=true` so
plain `X-Open-WebUI-*` identity headers are honored.

### 4. Install, migrate, run

```bash
uv sync --extra dev
uv run alembic upgrade head
uv run uvicorn gateway.app:create_app --factory --reload
```

To run the app in Docker against the host Postgres instead:

```bash
docker compose up --build
```

The compose service uses host networking so the container can reach the host
Postgres through `127.0.0.1`. The default `DATABASE_URL` is:

```text
postgresql+psycopg://workspace_mcp:wmcp_local_dev_pw@127.0.0.1:5432/workspace_mcp
```

Set `DOCKER_DATABASE_URL` in `.env` if your host Postgres uses a different port,
database, user, or password. The container runs `alembic upgrade head` before
starting Uvicorn.

To deploy on a server, run `scripts/deploy.sh` (fast-forwards `main`, rebuilds,
restarts, and waits for `/health`), or `DEPLOY_HOST=user@host DEPLOY_DIR=/path
scripts/deploy.sh` to run it over SSH.

Health checks:

```bash
curl localhost:8000/health   # liveness
curl localhost:8000/ready    # readiness (checks DB connectivity)
```

### 5. Connect a Google account

Open in a browser (with the identity header injected by a dev proxy, or with
`DEV_TRUST_ALL_ORIGINS=true` and a header-injecting browser extension):

```
http://localhost:8000/oauth/google/start
```

### 6. Call MCP tools

Open WebUI door (header identity + shared secret):

```bash
curl -sS localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'X-Gateway-Auth: <GATEWAY_SHARED_SECRET>' \
  -H 'X-Open-WebUI-User-Id: alice' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

`/mcp` remains the backward-compatible all-tools endpoint. Open WebUI admins can
also register product-specific tool servers on the same backend:

- `http://127.0.0.1:8000/mcp/calendar`
- `http://127.0.0.1:8000/mcp/drive`
- `http://127.0.0.1:8000/mcp/tasks`
- `http://127.0.0.1:8000/mcp/apex`
- `http://127.0.0.1:8000/mcp/apex-wealth`
- `http://127.0.0.1:8000/mcp/telegram`

Calendar exposes `system_get_current_time` plus `google_calendar_*`; Drive
exposes `system_get_current_time`, `google_drive_search_files`, and
`google_drive_get_file_metadata`; Tasks exposes `system_get_current_time` plus
`google_tasks_*`; Apex exposes `system_get_current_time` plus `apex_*`
(`apex_list_clients`, `apex_list_timesheets`, `apex_get_last_timesheet`,
`apex_create_invoice`, `apex_create_timesheet`, `apex_update_timesheet`) against the
[Apex invoicing/timesheet REST API](../apex/docs/api/invoice-rest.openapi.yaml),
authenticated with a per-user, workspace-scoped Apex API key (stored with
`apex_set_api_key` or `POST /connectors/apex/apikey`; `APEX_API_KEY` is only an
optional deploy-wide fallback) rather than per-user OAuth.

Apex Wealth (`/mcp/apex-wealth`) exposes `system_get_current_time` plus read-only
`apex_wealth_*` tools: `apex_wealth_list_accounts`, `apex_wealth_get_accounts_summary`
(cash and liabilities), `apex_wealth_get_investments_summary`,
`apex_wealth_get_positions`, `apex_wealth_list_manual_holdings`, and
`apex_wealth_get_net_worth_history`, against the
[Apex wealth REST API](../apex/docs/api/wealth-rest.openapi.yaml). Wealth data only
exists in PERSONAL workspaces, so it uses its own key, stored with
`apex_wealth_set_api_key` or `POST /connectors/apex-wealth/apikey`
(`APEX_WEALTH_API_KEY` is an optional fallback).

Telegram exposes `telegram_list_destinations` and `telegram_send_message`.
Each Open WebUI user connects one BotFather token through Settings →
Integrations; the token is validated and encrypted without entering model
context. Channel aliases are accepted only after the gateway verifies that the
bot is a channel administrator with permission to post.

Private Telegram settings routes use the same trusted headers as MCP:

- `GET /api/telegram/status`
- `PUT` / `DELETE /api/telegram/connection`
- `POST /api/telegram/destinations`
- `POST` / `DELETE /api/telegram/destinations/{alias}`

## Open WebUI Drive attachments

The trusted Open WebUI backend can use the private HTTP integration routes to
provide a search-first attachment selector without exposing Google credentials:

- `GET /api/drive/status`
- `GET /api/drive/files?q=&page_token=&page_size=`
- `GET /api/drive/files/{file_id}/content`

These routes require the same trusted Open WebUI identity headers and
`X-Gateway-Auth` shared secret as MCP. They search shared files and Shared
Drives, resolve shortcuts, export Google Docs/Sheets/Slides to DOCX/XLSX/PPTX,
and reject post-export downloads over 50 MB. The Open WebUI backend must set:

```text
ENABLE_WORKSPACE_MCP_GATEWAY_DRIVE=true
WORKSPACE_MCP_GATEWAY_URL=http://127.0.0.1:<gateway-port>
WORKSPACE_MCP_GATEWAY_SHARED_SECRET=<same value as GATEWAY_SHARED_SECRET>
```

The URL and secret are server-only; `/api/config` exposes only the enabled
feature boolean. A user may have one active Google account for V1. Reauthorizing
the same account extends its scopes; connecting a different account returns an
explicit conflict.

Native-client door (static bearer token, works from anywhere incl. the public proxy):

```bash
# Mint a token for a user (shown once):
python -m gateway.tokens create --user alice --name cursor-laptop
python -m gateway.tokens list --user alice
python -m gateway.tokens revoke <prefix>

curl -sS https://your-host/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Authorization: Bearer wmcp_...' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## OAuth for ChatGPT and headless MCP clients

OAuth is optional and disabled unless `CLERK_ISSUER` is configured. The existing
Open WebUI trusted-header route and `wmcp_` static tokens continue to work. The
same six product mounts and `/mcp` are protected resources; HTTPS nginx forwards
`/.well-known/oauth-protected-resource[/mcp[/<product>]]` and OAuth callbacks to
the gateway. OAuth never carries Google, Apex, or Telegram credentials to the
client. A verified Clerk issuer and subject identify the same gateway user
across ChatGPT and CLI clients. The user is created on first use. Existing Open
WebUI provider connections can be reused through an explicit account link.

1. In a Clerk tenant, enable Google sign-in, OAuth, JWT access tokens, and a
   custom `mcp:tools` scope. Advertise this scope and assign it to the OAuth
   applications. Enable CIMD for clients that support it (including ChatGPT);
   enable DCR only if a required client lacks CIMD, because DCR allows public
   unauthenticated client registration. Set the default scopes for clients that
   omit `scope` to include `mcp:tools`. Require PKCE S256 for authorization-code
   clients, and keep consent enabled.
   Register every MCP URL clients will use as a Clerk MCP server resource and
   enable the OAuth audience claim (`aud_claim_enabled`). Clerk's management
   MCP provides `configure_mcp_server`: preview with `dry_run: true`, then save
   with `dry_run: false`. Register `/mcp` and any product URLs separately because
   the gateway checks the exact resource audience. The first resource setup can
   enable the audience claim; verify it is enabled after saving. Clients with
   tokens issued before this change must complete a fresh authorization flow.
2. For optional reuse of an existing Open WebUI account, create a
   **confidential** Clerk OAuth application for account linking. Allow
   `openid profile mcp:tools` and register the exact callback
   `https://mcp.ashpazi.shop/oauth/clerk/link/callback` (replace the hostname if
   your `BASE_URL` differs). Set `CLERK_ISSUER` to the Clerk HTTPS Frontend API
   issuer and set `CLERK_LINK_CLIENT_ID` and `CLERK_LINK_CLIENT_SECRET` in the
   gateway's private `.env`. Do not put the secret in Open WebUI or a CLI.
3. Rebuild/restart the gateway after updating its dependencies and run the
   Alembic `0004_oauth_identity_links` migration. If reusing an Open WebUI
   account, enable `ENABLE_WORKSPACE_MCP_GATEWAY_OAUTH_LINKING=true` in its
   backend, rebuild the fork, and restart it. The signed-in user then goes to
   Settings → Integrations → External MCP access, clicks **Link OAuth login**,
   completes Clerk sign-in, and refreshes the status. Removing the link stops
   access to that Open WebUI user's provider connections; the Clerk identity
   can still use its own gateway account.
4. Connect a client to `https://mcp.ashpazi.shop/mcp` or a product URL such as
   `/mcp/telegram`. The gateway advertises Clerk authorization-server discovery,
   `mcp:tools` tool security schemes, and a `WWW-Authenticate` challenge. The
   authorization-code flow is handled by the client and Clerk, not by Open
   WebUI. For ChatGPT, enable Developer mode and add the HTTPS MCP URL as a
   connector. Test both list and mutating tools with a Clerk test user.

For a headless CLI, create a separate **public** Clerk OAuth application with
Device Authorization Grant and `mcp:tools` enabled. It has no client secret.
The helper below prints a verification URL and user code on stderr, polls at
Clerk's advertised interval, and writes only the access token to stdout. The
user completes approval on another browser-capable device. A CLI with native
OAuth device-flow support should use Clerk's advertised
`device_authorization_endpoint` directly and manage refresh tokens in its own
secure credential store. Fully unattended jobs can continue using revocable
`wmcp_` tokens; device-code login itself always requires user approval.
The helper supports `--offline-access --json` when a CLI needs to capture a
refresh token; treat that JSON as a secret and store it with owner-only access.

```bash
umask 077
.venv/bin/python scripts/mcp_oauth_device.py \
  --issuer 'https://YOUR_CLERK_FRONTEND_API' \
  --client-id 'YOUR_PUBLIC_DEVICE_CLIENT_ID' \
  --resource 'https://mcp.ashpazi.shop/mcp/telegram' \
  > /path/to/secure-token-output
```

**Current ChatGPT status (2026-10-25):** The user confirmed tools are available
after reconnecting and `system_get_current_time` returned the time successfully.
The earlier action-discovery failure was caused by missing
Clerk MCP resource registration and a disabled audience claim. Using Clerk's
management MCP, we registered `/mcp` and all six product URLs in the development
instance at `https://robust-ibex-65.clerk.accounts.dev` and enabled
`aud_claim_enabled`. The gateway's strict audience validation remains enabled.
Provider-specific actions and CLI device-flow verification are separate checks; see
[the investigation summary](docs/oauth-status-and-issues.md).

**Release gate:** Before relying on Clerk OAuth, verify a real
authorization-code token and a real device-code token have JWT `iss` equal to
`CLERK_ISSUER`, `aud` equal to the exact MCP resource URL, and `scope` containing
`mcp:tools`. The gateway intentionally rejects client-ID-only or unbound
audiences. Clerk resource registration and the audience-claim setting must be
configured before this test. If the test fails, investigate the resource,
audience setting, and token issuance flow without weakening audience validation.
With the gateway's Clerk settings present, check a token without displaying
it:

```bash
.venv/bin/python scripts/check_oauth_token.py \
  --resource 'https://mcp.ashpazi.shop/mcp/telegram' \
  < /path/to/secure-token-output
```

## Security model

Three front doors, all resolving to the same user and provider connections:

- **Open WebUI (header identity).** `X-Open-WebUI-User-Id/Email/Name` are trusted
  **only** when the request also presents the `X-Gateway-Auth` shared secret
  (`GATEWAY_SHARED_SECRET`), compared in constant time. Open WebUI must reach the
  gateway directly (e.g. `127.0.0.1:8000`), bypassing the public proxy so its
  headers survive. The public proxy strips `X-Gateway-Auth` and `X-Open-WebUI-*`,
  so a public caller can never assert header identity.
- **Native clients (bearer token).** Cursor / Claude Desktop authenticate with a
  per-user token minted by `python -m gateway.tokens`. Only the token's SHA-256
  hash is stored; revoke via `revoke`. A token maps to a `User`, reusing any
  Google connection made through Open WebUI. Users without a Google connection get
  an actionable `/oauth/google/start?ticket=...` link in the tool error.
- **OAuth clients.** Clerk access tokens must have a valid signature, issuer,
  expiry, exact resource audience, and `mcp:tools` scope. The verified issuer
  and subject map to one gateway user across OAuth clients. An explicit link
  lets that user reuse an existing Open WebUI user's provider connections;
  email similarity never creates a link. Unlink removes only that mapping, not
  the Open WebUI user's provider connections.
- **Deployment.** Terminate TLS at the proxy (`SSL_CERT`/`SSL_KEY` in
  `scripts/setup-nginx.sh`); bind the app to `127.0.0.1` and firewall its port so
  the only public route is through the proxy.
- Tokens are encrypted at rest with Fernet (`TOKEN_ENCRYPTION_KEY`).
- Tool calls are audited (`tool_audit_log`, incl. `auth_source`) without logging
  tokens, file contents, or full event descriptions.

## Testing

```bash
uv run pytest
```
