# Workspace MCP Gateway

A multi-user MCP gateway for [Open WebUI](https://github.com/open-webui/open-webui).
Each Open WebUI user connects their own Google Workspace account; the gateway
exposes Streamable HTTP MCP endpoints and keeps provider integrations modular.

This is the **V1 vertical slice**: gateway skeleton, Postgres schema, Google
OAuth with encrypted token storage, and Google **Calendar** tools. See
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

Calendar exposes `system_get_current_time` plus `google_calendar_*`. Drive and
Tasks currently expose only system tools until their provider modules are wired.

Native-client door (bearer token, works from anywhere incl. the public proxy):

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

## Security model

Two front doors, both resolving to the same user and Google connection:

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
