# Deployment & Open WebUI Integration Runbook

Operational notes for running `workspace-mcp-gateway` behind nginx, connected to a
multi-user Open WebUI on the same host. Captures the real setup on this server,
the gotchas we hit, and the remaining steps.

Last updated: 2026-05-31.

---

## 1. Topology (this server)

```
Browser (users) ──https──> nginx :443 (mcp.ashpazi.shop) ──> gateway 127.0.0.1:8000
                                  │  strips X-Gateway-Auth + X-Open(-)WebUI-User-* on public path
Open WebUI (ai_open_webui) ──http──> 127.0.0.1:8000/mcp/* (host network, BYPASSES nginx;
                                  identity + secret headers survive)
Cursor / Claude (future) ──https──> nginx :443 ──> gateway  (Authorization: Bearer wmcp_…)
```

- **Gateway**: docker compose project at `/home/orion/workspace-mcp-gateway`
  (`docker-compose.yml`), container `workspace-mcp-gateway-app-1`, `network_mode: host`,
  listens on `0.0.0.0:8000`. Migrations run on container start (`docker-entrypoint.sh`).
- **Open WebUI**: docker compose project `ai-chat-stack` at `/home/orion/ai/docker-compose.yml`,
  container `ai_open_webui`, `ghcr.io/open-webui/open-webui:main` (v0.9.5), host network,
  UI on `:8080`. Its own Postgres DB (`openwebui`).
- **nginx**: host install, sites in `/etc/nginx/sites-enabled/` (`workspace-mcp-gateway`).
  Public hostname **`mcp.ashpazi.shop`** → `173.33.32.125` (this server). `certbot` installed.
- **Gateway Postgres**: `workspace_mcp` DB on `127.0.0.1:5432`.

---

## 2. Auth model (two front doors)

Client authentication is separate from the per-user Google connection. Both doors
resolve to the same `users` row and reuse the same Google tokens.
Single chokepoint: `src/gateway/identity/resolver.py`.

1. **Open WebUI — header identity, gated by a shared secret.**
   `X-Open-WebUI-User-*` / `X-OpenWebUI-User-*` are trusted **only** when the request
   also carries `X-Gateway-Auth: <GATEWAY_SHARED_SECRET>` (constant-time compare). The
   public nginx path strips that header, so only the on-host (loopback) Open WebUI path
   can assert identity.
2. **Native clients — per-user bearer tokens.** `Authorization: Bearer wmcp_…`, minted
   by the CLI, hashed at rest. Works from any path including the public proxy.

Audit rows record which door was used (`tool_audit_log.auth_source`).

---

## 3. Gateway configuration (`.env`)

File: `/home/orion/workspace-mcp-gateway/.env` (gitignored — holds secrets).
Compose passes these through (`docker-compose.yml`). Required keys:

| Key | Purpose |
|---|---|
| `DATABASE_URL` | gateway Postgres |
| `BASE_URL` | `https://mcp.ashpazi.shop` — derives the OAuth redirect URI |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google OAuth web client |
| `TOKEN_ENCRYPTION_KEY` | Fernet key, encrypts provider tokens at rest |
| `GATEWAY_SHARED_SECRET` | the `X-Gateway-Auth` value Open WebUI sends |
| `TRUSTED_OPEN_WEBUI_ORIGIN` | defense-in-depth only (not the trust gate) |
| `SESSION_SECRET` | signs OAuth state + connect tickets |
| `DEV_TRUST_ALL_ORIGINS` | `false` in prod |

Apply env changes: `docker compose up -d`. Python source and migrations are
bind-mounted into the app container, so code/migration changes need a container
restart but not an image rebuild. Dependency, Dockerfile, or entrypoint changes
still need `docker compose up -d --build`.

---

## 4. Open WebUI integration

Add the gateway as **MCP (Streamable HTTP)** tool servers in **Admin Panel →
Settings → Integrations → Tool Servers**. During migration, `/mcp` remains the
all-tools endpoint:

- **Type:** **MCP** (NOT OpenAPI — default is OpenAPI; the wrong type makes it fetch
  `/openapi.json` and load zero tools).
- **URL:** `http://127.0.0.1:8000/mcp` (note the **:8000** — without it you hit nginx on :80).
- **Auth:** None.
- **ID:** any unique slug, e.g. `workspace-mcp`.
- **Access control:** **Public** (empty access grants = admin-only; users won't see it).
- **Headers (JSON):**
  ```json
  { "X-Gateway-Auth": "<GATEWAY_SHARED_SECRET from .env>" }
  ```

For product-level selection in Open WebUI, register separate tool servers against
the same backend and same static header:

| Name | URL |
|---|---|
| `Google Calendar` | `http://127.0.0.1:8000/mcp/calendar` |
| `Google Drive` | `http://127.0.0.1:8000/mcp/drive` |
| `Google Tasks` | `http://127.0.0.1:8000/mcp/tasks` |

`/mcp/calendar` exposes `system_get_current_time` plus `google_calendar_*`.
`/mcp/drive` and `/mcp/tasks` are mounted now but expose only system tools until
their provider modules exist.

### The identity gotcha (important)

Open WebUI v0.9.5 does **not** substitute `{{USER_ID}}` template vars in custom headers
on the **MCP runtime path** (only on the OpenAPI path / connection-test). It sends them
verbatim → every user arrived as the literal string `{{USER_ID}}`.

**Fix applied:** real per-user identity comes from Open WebUI's user-info forwarding,
enabled in `/home/orion/ai/docker-compose.yml` on the `open-webui` service:

```yaml
ENABLE_FORWARD_USER_INFO_HEADERS: "true"
```

With that flag, Open WebUI forwards `X-OpenWebUI-User-Id/Name/Email` (one word
"OpenWebUI") with real values. The gateway was updated to accept **both** spellings
(`x-openwebui-user-*` and `x-open-webui-user-*`), preferring the real forwarded value
(`src/gateway/identity/resolver.py`, `_first_header`). So custom headers only need the
static `X-Gateway-Auth`; do not put templated user headers there.

Restart Open WebUI after env changes:
```bash
docker compose -f /home/orion/ai/docker-compose.yml --project-directory /home/orion/ai up -d open-webui
```

Open WebUI connects to the MCP server **lazily** (when a user runs a tool in chat),
not on save — so no gateway traffic appears until first use.

---

## 5. Per-user Google connection flow

Each user authorizes their own Google account:

1. User runs a calendar tool (e.g. "list my Google calendars").
2. With no connection, the gateway returns a `not_connected` error containing a personal
   link: `https://mcp.ashpazi.shop/oauth/google/start?ticket=<signed user id>`.
3. User opens the link → Google consent → callback stores encrypted tokens server-side.
4. Re-running the tool now returns real data.

The signed `ticket` (HMAC via `SESSION_SECRET`, 7-day TTL) lets the browser — which
carries no identity headers — bind the OAuth flow to the right user.

---

## 6. Google OAuth client

Google Cloud Console → **APIs & Services**:

- Enable **Google Calendar API**.
- **OAuth consent screen / "Google Auth Platform"**: set **Audience** (Internal if all
  users are on the instacart.com Workspace; External + add Test users otherwise).
- **Data Access** → add scopes: `openid`, `userinfo.email`, `userinfo.profile`,
  `calendar.readonly`, `calendar.events`.
- **Clients → Create client → Web application**, Authorized redirect URI **exactly**:
  ```
  https://mcp.ashpazi.shop/oauth/google/callback
  ```
- Put the Client ID/secret in `.env`, then `docker compose up -d`.

Scopes requested by the current build live in `src/gateway/oauth/google.py`
(`DEFAULT_SCOPES`). Common errors: `redirect_uri_mismatch` (URI not exact),
`access blocked / app not verified` (External+Testing without the user as a Test user).

---

## 7. TLS / nginx  ← CURRENT BLOCKER

`BASE_URL` is `https://`, but nginx has **no cert for `mcp.ashpazi.shop`** yet, so the
OAuth link fails with `ERR_SSL_VERSION_OR_CIPHER_MISMATCH`. `certbot` is installed; DNS
and port 80 are confirmed reachable. **Run (needs sudo password):**

```bash
# 1) Issue the certificate (ACME challenge served via nginx on :80)
sudo certbot certonly --nginx -d mcp.ashpazi.shop --agree-tos -m moe.faraji@instacart.com

# 2) Regenerate the nginx site WITH TLS (443 + 80→443 redirect + header strips)
sudo SERVER_NAME=mcp.ashpazi.shop \
  SSL_CERT=/etc/letsencrypt/live/mcp.ashpazi.shop/fullchain.pem \
  SSL_KEY=/etc/letsencrypt/live/mcp.ashpazi.shop/privkey.pem \
  bash /home/orion/workspace-mcp-gateway/scripts/setup-nginx.sh
```

`scripts/setup-nginx.sh` (updated) emits the TLS server block, the HTTP→HTTPS redirect,
and strips `X-Gateway-Auth` + `X-Open-WebUI-User-*` on the public path. Cert auto-renews
via certbot's timer; nginx reload on renew may need a deploy hook.

If command 1 says "could not find a VirtualHost", the site's `server_name` isn't
`mcp.ashpazi.shop` — use the `--webroot` variant instead.

---

## 8. Native clients (Cursor / Claude) — future

```bash
docker compose exec app uv run --no-sync python -m gateway.tokens create --user <external_user_id> --name cursor-laptop
docker compose exec app uv run --no-sync python -m gateway.tokens list --user <external_user_id>
docker compose exec app uv run --no-sync python -m gateway.tokens revoke <prefix>
```
Configure the printed token in the client as `Authorization: Bearer wmcp_…`, URL
`https://mcp.ashpazi.shop/mcp`. The same user id reuses the Google connection made via
Open WebUI. `create` also prints a one-time Google connect link.

---

## 9. Remaining steps / TODO

1. **[blocker] Issue TLS cert + apply** (section 7), so `https://mcp.ashpazi.shop` serves.
2. **End-to-end test**: in an Open WebUI chat, "list my Google calendars" → open the
   connect link → authorize Google → re-run and confirm real data.
3. **Consent screen test users** (if External): add each Open WebUI user's Google email.
4. **Clean up** the MCP connection's headers in Open WebUI to just `X-Gateway-Auth`
   (the leftover `{{USER_ID}}`/`{{USER_NAME}}` headers are now ignored but untidy).
5. **Hardening**: gateway binds `0.0.0.0:8000`; firewall port 8000 so the only public
   route is via nginx (auth still holds either way — secret/token required).
6. **Commit** the `docker-compose.yml` `GATEWAY_SHARED_SECRET` passthrough (the merged
   PR #9 missed it). Branch was `feat/secure-mcp-endpoint-8`; issue #8 / PR #9.

---

## 10. Diagnostics cheat-sheet

```bash
# Gateway
docker compose -f /home/orion/workspace-mcp-gateway/docker-compose.yml logs --tail=40 app
docker compose -f /home/orion/workspace-mcp-gateway/docker-compose.yml logs -f --tail=0 app | grep -iE "/mcp|401|CallTool|ListTools"
curl -sS http://127.0.0.1:8000/health

# Verify the Open WebUI door (replace SECRET)
curl -sS http://127.0.0.1:8000/mcp/ -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' -H "X-Gateway-Auth: SECRET" \
  -H 'X-Open-WebUI-User-Id: alice' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# Audit log (who/what/how authed)
docker compose exec app uv run --no-sync python -c "from gateway.db.engine import session_scope; from sqlalchemy import text;\
import itertools;\
[print(r) for r in session_scope().__enter__().execute(text('select created_at,tool_name,auth_source,result_status,error_code from tool_audit_log order by created_at desc limit 10'))]"

# Open WebUI
docker logs --tail=40 ai_open_webui
docker exec ai_open_webui sh -lc 'echo $ENABLE_FORWARD_USER_INFO_HEADERS'

# nginx / TLS
sudo nginx -T | grep -A20 mcp.ashpazi.shop
sudo certbot certificates
curl -sSI https://mcp.ashpazi.shop/oauth/google/start
```

---

## 11. Key facts we discovered

- Open WebUI 0.9.5 MCP runtime headers are built in `utils/middleware.py` (~line 2700):
  custom headers copied **verbatim** (no templating); user info added only if
  `ENABLE_FORWARD_USER_INFO_HEADERS` is true, via `utils/headers.py:include_user_info_headers`.
- Forwarded header names default to `X-OpenWebUI-User-{Id,Name,Email}` (configurable via
  `FORWARD_USER_INFO_HEADER_USER_*`). Gateway now accepts both these and the hyphenated form.
- `POST /mcp` 307-redirects to `/mcp/`; clients (and Open WebUI's httpx) follow it.
- Open WebUI tool-server access defaults to admin-only until access grants are set to public.
- The gateway's MCP endpoint requires `Accept: application/json, text/event-stream`.
