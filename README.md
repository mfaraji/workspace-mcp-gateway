# Workspace MCP Gateway

A multi-user MCP gateway for [Open WebUI](https://github.com/open-webui/open-webui).
Each Open WebUI user connects their own Google Workspace account; the gateway
exposes a single Streamable HTTP MCP endpoint and keeps provider integrations
modular.

This is the **V1 vertical slice**: gateway skeleton, Postgres schema, Google
OAuth with encrypted token storage, and Google **Calendar read** tools. See
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

```bash
curl -sS localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'X-Open-WebUI-User-Id: alice' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Security model

- Identity is taken from `X-Open-WebUI-User-Id/Email/Name` headers, trusted only
  when the request originates from `TRUSTED_OPEN_WEBUI_ORIGIN`. **In production,
  run behind a reverse proxy that strips client-supplied `X-Open-WebUI-*` and
  `X-Forwarded-*` headers** so they cannot be spoofed.
- Tokens are encrypted at rest with Fernet (`TOKEN_ENCRYPTION_KEY`).
- Tool calls are audited (`tool_audit_log`) without logging tokens, file
  contents, or full event descriptions.

## Testing

```bash
uv run pytest
```
