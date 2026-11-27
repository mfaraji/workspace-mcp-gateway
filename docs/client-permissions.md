# OAuth client permissions

Every connector is served separately at `/mcp/<slug>`, including calendar,
drive, tasks, telegram, apex, apex-wealth, apex-transactions, finance, and recipes. `/mcp` remains
a compatibility endpoint. All mounts share one backend and require OAuth.
Personal tools require a grant for the verified user and OAuth application.
Shared read-only tools (Finance) are available to authenticated users. Every
endpoint exposes only its own tools.

## Breaking cutover

Configure CLERK_ISSUER, CLERK_BROWSER_CLIENT_ID and CLERK_BROWSER_CLIENT_SECRET.
The browser client is confidential; register `{BASE_URL}/access/callback`, enable
authorization-code flow with PKCE and `openid profile mcp:tools`. Configure Clerk
to issue signed, audience-bound access tokens containing `client_id` or `azp`.
Register `/mcp` and all product resources in Clerk. Serve over HTTPS.
Run `.venv/bin/alembic upgrade head` before starting the updated application.
Migration 0005 creates grants and revokes old static tokens. Downgrade does not
reactivate credentials. Existing provider connections and identity links survive.
Unlinked users must reconnect providers; accounts are never merged by email.

Register each desired connector endpoint in Open WebUI using OAuth 2.1 (with a
registered Open WebUI OAuth client when static client registration is needed).
The standard **Register** button requires Clerk OAuth applications → Settings →
Client onboarding → **Publish DCR support**. This corresponds to
`dynamic_oauth_client_registration: true` in instance OAuth application settings.
It is instance-wide and explicitly enables public OAuth client registration;
registration itself gives no access to user data. The Clerk authorization-server
metadata must advertise its `/oauth/register` endpoint. The gateway does not host
a `/register` endpoint. If Open WebUI falls back there and receives 404, check this
setting and refresh cached issuer metadata. Existing pre-registered clients remain
valid; use those when public registration is intentionally disabled.
Remove shared-key headers. Existing key/header/token integrations immediately
stop working, including older Open WebUI provider-settings proxies. Private
provider APIs now require the gateway browser session; cookie-authenticated
writes require X-CSRF-Token from GET /api/access. They do not accept client MCP
tokens, preventing provider API calls from bypassing tool grants.

## Overarching endpoints

Besides the per-connector endpoints, `/mcp/google` serves every Google connector (Calendar,
Drive, Tasks, Gmail) from one registration. It owns no tools or credentials: it advertises
the union of its members' tools, and grants, audit and Gmail's label/draft-only limits work
exactly as on the separate endpoints. New Google connectors join it automatically. Register
`{BASE_URL}/mcp/google` as a resource in Clerk like the other product resources; the consent
page for it lists all member tools. The separate `/mcp/<slug>` endpoints are unchanged, and
`/mcp` remains the everything-endpoint. Prefer a separate endpoint when you want one service
(for example Gmail) switched on or off on its own, or to keep fewer tools in the model's context.

## User flow

1. Connect a product MCP endpoint through the client's normal OAuth control.
2. Sign in through Clerk. Clerk sends the browser to `/oauth/consent` on the gateway.
3. Review the requesting application, account, redirect destination and OAuth scopes.
   Select tools for the requested connector; write access remains opt-in.
4. Allow saves the explicit tool grant and submits the original authorization
   request to Clerk. Clerk validates the redirect/PKCE and returns an authorization
   code to the client's existing callback. Deny returns `access_denied`, without
   changing tool grants. No client-specific callback hooks or shared keys are used.
5. The client exchanges the code normally. Tool discovery and calls enforce the grant.

`/access` remains available for later grant management and fallback setup links.

The setup link expires after one hour and cannot approve access by itself.
`/access` lists existing client grants for editing or revocation. An empty grant
denies all connector tools. New tools require explicit approval. Changes apply
to subsequent requests, including existing sessions; already running calls may
finish. Write access retains existing confirmation requirements.

### Risk level (low / medium)

Each write tool has a risk level the user can change on `/access`, independent of
which client is connected (it is per user, not per grant). **Low** tools run
directly. **Medium** tools return `confirmation_required` with a plain-language
`question`; the assistant must put that question to the user in chat and only
after an explicit yes call the tool again with the `confirmation_token` (valid for
`CONFIRMATION_TOKEN_TTL_SECONDS`, 600). Defaults: tools that are recoverable
(tasks, drafts, recipe saves) are low; deletes, calendar changes and Apex invoice
actions (creating, sending, marking paid) are medium. Read tools have no level.

PUT /api/tool-risk accepts `{ "tool": "google_tasks_create_task", "tier": "medium" }`
(CSRF required); GET /api/access returns the user's overrides under `risk`.

GET /api/access returns the connector/tool catalog, existing grants, active setup
client, and CSRF token to an authenticated browser. PUT /api/access/{client_id}
accepts `{ "tools": ["google_calendar_list_events"] }`; DELETE revokes it.
Only signed setup links permit creation of a grant for a new client. These
management APIs reject ordinary MCP tokens and require CSRF for mutation.

Provider credentials remain encrypted and are separate from client authentication.
Health/readiness endpoints remain unauthenticated. Verify real Open WebUI OAuth,
selection, allowed reads, denied writes, and revocation after deployment.

## Connector-scoped consent

Setup links carry the exact verified MCP resource audience. A Telegram link
shows only Telegram tools and the requesting client. The save API rejects tools
outside that connector. Saving or removing its access preserves grants for other
connectors on the same OAuth application. `/mcp` links remain the all-connectors
management flow. Links generated before resource binding must be regenerated by
refreshing/reconnecting the MCP client and requesting its permissions link again.
The permissions page serves its own stylesheet with mobile and dark-mode support.

## Native OAuth consent configuration

Set CLERK_PUBLISHABLE_KEY and the matching instance's CLERK_BACKEND_API_KEY on the
gateway. Set CLERK_SESSION_AUDIENCE to the configured browser-session audience
when the instance customizes it. These are server configuration, not client keys.
Configure Clerk Paths -> OAuth consent as `/oauth/consent` on the gateway origin.
On a development instance, configure its development origin as the gateway URL;
production consent URLs must satisfy Clerk's same-domain requirements. Keep
consent enabled for OAuth applications. The SDK is pinned to ClerkJS 6.42.3; standard consent uses Clerk UI 1.42.4.

The consent API verifies a browser-session JWT (issuer/signature/expiry, session ID,
configured audience and exact authorized origin) and rejects OAuth access tokens.
Client registration, requested scopes and redirect URI are independently checked
against Clerk before issuing a ten-minute signed consent intent. Approval is bound
to that subject, client and connector. A product grant update preserves other
connector grants. Unrelated OAuth requests and the gateway's own browser-login
client use Clerk's prebuilt consent component, without tool-grant changes.

Gateway application access logs omit query strings. The deployed nginx gateway
site also uses a query-free log format, since OAuth URLs may include browser
credentials in development. Authorization codes and tokens must never be logged.

## Shared data connectors

Connector access policy is explicit: `AccessMode.PERSONAL` (the default) requires
per-user, per-client tool grants; `AccessMode.AUTHENTICATED_SHARED` makes generic
read-only tools available to every authenticated OAuth caller. Finance uses shared
access. Recipes remains personal because its documents belong to individual users,
even though it has no upstream sign-in.

Shared access still requires valid OAuth scope and the correct resource audience.
It skips the gateway tool picker and uses Clerk's standard OAuth consent when
required. The mixed `/mcp` endpoint exposes shared tools plus granted personal tools;
its consent screen labels shared connectors as available to signed-in users.
Discovery and execution use the same authorization decision, and calls retain normal
caller-scoped auditing. Startup rejects shared connectors containing mutating tools.

Shared tools cannot be selected or revoked through personal grant APIs. Old grant
entries for shared tools have no effect. Disconnect the client or revoke its OAuth
authorization to remove its access. No database migration is required.
