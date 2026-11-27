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

## Interactive widgets (MCP Apps)

Tools may render widgets in clients that support MCP Apps. Widgets live in `src/gateway/mcp/ui/` and are
served as `ui://` resources gated by the caller's tool grants; see `docs/mcp-apps.md` before adding one.
Widget actions go through the normal tool pipeline, so never add a widget-only write path.
Widgets are off unless `MCP_APPS_ENABLED=true`; when off no endpoint advertises `_meta.ui` or `ui://` resources.

## Product MCP endpoint convention

Keep one backend gateway process, but expose product-specific MCP mounts so Open
WebUI admins can register separate tool servers and users can select products
with Open WebUI's native tool-server UI:

- `/mcp` remains the backward-compatible "all enabled tools" endpoint.
- `/mcp/calendar` registers `system_get_current_time` plus exactly Google's Calendar MCP
  tool set under Google's bare names (`get_event`, `list_calendars`, `list_events`,
  `search_events`, `suggest_time`, `create_event`, `update_event`, `delete_event`,
  `respond_to_event`; see `Connector.google_names`) and its five prompts
  (`schedule_meeting`, `quick_meeting_today`, `recurring_1on1`, `team_meeting_scheduler`,
  `analyze_availability`, in `mcp/calendar_prompts.py`). The bare names are an alias layer:
  grants, tiers and audit still use the internal `google_calendar_*` names, and `/mcp` and
  the `/mcp/google` bundle keep the prefixed names, `google_calendar_list_accounts` and
  widgets. `respond_to_event` notifies the organizer, so it defaults to MEDIUM.
- `/mcp/drive` registers `system_get_current_time` plus exactly Google's Drive MCP tool set
  under Google's bare names (`search_files`, `read_file_content`, `share_file`, ...); the
  internal specs stay `google_drive_*` for grants, tiers and audit (`Connector.google_names`),
  and `/mcp` keeps the prefixed names. Reads use the read-only scopes; the write tools
  (create/copy/update/trash/share/request_access) need the full `drive` scope and return
  `reauth_required` until the user reconnects. `share_file` only grants reader/commenter/
  writer to a named user or group (no link/domain sharing, no ownership transfer);
  `trash_file` is recoverable and nothing is permanently deleted. `download_file_content`
  and `create_file(file_ref=...)` move files through `staged_files` handles, never the model.
- `/mcp/tasks` registers `system_get_current_time` plus `google_tasks_*` when
  Tasks provider modules exist; until then it intentionally exposes only system
  tools.
- `/mcp/gmail` registers `system_get_current_time` plus `google_gmail_*`. Reads and
  searches are scoped per connected Gmail account to what the user shares: by
  default only messages with the `GMAIL_ACCESS_LABEL` label (`agent-access`), or chosen
  labels, or all mail (`connection_metadata["gmail_sharing"]`, set only from the browser
  `/connections` page via session + CSRF, "all mail" needs explicit confirmation, never by a
  model-visible tool; unshared = `not_found`). Writes are drafts only
  (HTML supported); there is no send tool, and update/delete only touch drafts
  recorded in `gmail_agent_drafts`. `google_gmail_download_attachment` (label-scoped like
  reads, max 10 MB) never returns bytes: it stores the file encrypted in `staged_files`
  (15 min, single use, owner-only) and returns a `file_ref` for other tools.
  Google parity (`providers/google/gmail/manage.py`): labels (create/update/delete,
  label/unlabel message and thread, `update_message_labels`), trash/untrash, spam/unspam and
  `apply_sensitive_*_label` (applies `GMAIL_SENSITIVE_LABEL`, default `Sensitive`; our reading
  of Google's tool). Every write checks the message is shared (unshared = `not_found`); thread
  tools act on the shared messages of a thread one by one; the sharing label(s) can never be
  added, removed, renamed or deleted by a tool, and SPAM/TRASH/DRAFT/SENT cannot be set via
  label tools. These need `gmail.modify` (or `gmail.labels` for label CRUD): connections that
  predate it keep reading/drafting and get `reauth_required` only on these tools. Still no
  send/reply/forward. The own `/mcp/gmail` mount exposes exactly Google's tool names (bare, no
  widgets; see `Connector.google_names`); `/mcp` and `/mcp/google` keep `google_gmail_*`.
- `/mcp/telegram` registers `system_get_current_time`,
  `telegram_list_destinations`, and `telegram_send_message`. Bot credentials are
  configured through the private HTTP API, never through a model-visible tool.
- Apex is two products. **Apex Finance** (`/mcp/apex`) is the personal finance app:
  bookkeeping (`apex_*`: invoices, timesheets, expenses) and spending questions
  (`apex_transactions_*`). **Apex Invest** (`/mcp/apex-wealth`) is wealth and investments.
  `/mcp/apex-wealth` registers `system_get_current_time` and read-only `apex_wealth_*`
  tools (wealth data only exists in PERSONAL workspaces); `/mcp/apex` excludes these via
  `Connector.exclude_prefixes`. `/mcp/apex-transactions` is a hidden alias of `/mcp/apex`
  (`Connector.hidden`/`alias_of`) kept for existing clients; it owns no tools and is not
  listed on the access or connections pages. Workspaces are listed by
  `apex_list_workspaces` for every Apex Finance tool.
- The spending tools (`apex_transactions_*`, `providers/apex/transactions.py`):
  `find_categories`, `summarize`, `search`, `find_merchants` and rental-only
  `list_properties`. They wrap Apex's transactions query REST API (scope
  `apex:transactions.read`; capability `transactionInsights`, `rentalBooks` for
  properties). Apex does no natural-language parsing, so the tool descriptions carry the
  workflow (find the category id in English first, then summarize, then search for the
  rows) and every answer should state `interpretation.period` and mention `leftOut`.
  Never compute totals in the model or the gateway: Apex returns them (spending positive,
  net of refunds, CAD; BUSINESS is the business share, RENTAL is allocated slices with
  capital and personal use kept out). The audit log records ids, presets and enums only,
  never payee/search text, tags, amounts or dates. `/mcp/apex-wealth` excludes these tools.
- Apex has no keys, no stored credentials and no default workspace. The user's Clerk
  token is audience-bound to the gateway and is **never forwarded** (MCP token
  passthrough is forbidden). For each call `gateway.providers.apex.client.apex_credentials`
  validates `workspace_id` against the user's workspaces and mints a short-lived,
  Apex-only JWT with `gateway.oauth.downstream.mint_apex_token` (`sub` = verified Clerk
  subject, `act` = this gateway, `scope` = the tool's scope, `wid` = the workspace,
  `aud` = `APEX_MCP_AUDIENCE`), signed with `GATEWAY_SIGNING_KEY`; Apex verifies it
  against `/.well-known/apex-jwks.json`. Never log or audit minted tokens (only `jti`,
  scopes, `workspace_id`).
- Expense tools (`providers/apex/expenses.py`): `apex_list_expense_categories` is user-level
  (`apex_user_credentials`, scopes `apex:expenses.read|write`). `apex_create_expense` and
  `apex_upload_expense_receipt` **require `workspace_id`** (from `apex_list_workspaces`),
  validated with `apex_credentials(capability="receiptReview")`; it is sent to Apex
  (`workspaceId` in the body / `?workspaceId=` on the receipt upload, and inside the upload
  ticket) so the AI scan categorizes against that workspace. Apex books the expense right
  away as a NEEDS_REVIEW transaction in that workspace (counted as valid, highlighted in
  expense lists, also shown in the user's Apex inbox; rental workspaces link it to their
  default property; `also_workspace_ids` links it to further workspaces, e.g. rental +
  business) once amount and date are known (given or AI-scanned from the receipt). Optional
  `business_share_percent` (default 100) and `gst_percent` (GST/HST included in the amount,
  default 13, 0 = none) are sent as `businessSharePercent`/`gstPercent` and saved on business
  workspaces (native or linked). Rejecting
  in the inbox moves it to the Apex trash. LOW tier because the review is the human gate. Receipts arrive via `receipt_file_ref` (from the Gmail download) or
  `apex_upload_expense_receipt`, which without a `file_ref` returns a signed single-use
  ticket for `POST /uploads/apex-receipt` (bearer ticket, 10 min, never logged).
- Every workspace-scoped Apex tool takes a required `workspace_id` (from
  `apex_list_workspaces` / `apex_wealth_list_workspaces`). A tool is *permitted* when
  one of the caller's Apex workspaces has its `ToolSpec.required_capability`
  (`ApexUpstream.permits`, workspaces cached ~60s per Clerk subject). Permission is
  checked at discovery (`tools/list`, widgets), on the consent and `/access` pages
  (`policy.permitted_catalog`) and at call time (`policy.enforce` -> `forbidden`), always
  intersected with the client grant. Apex re-checks membership and capability itself.
- `/mcp/finance` (shown as "Market") registers `system_get_current_time` plus `finance_*` read-only
  exchange-rate tools (public API, no per-user credentials; default quote IRR).
- `/mcp/recipes` registers `system_get_current_time` plus `recipes_*` tools
  (get/list/search/save/update/delete plus add/delete feedback) backed by Couchbase Capella. Capella is
  optional: without `CAPELLA_*` settings the tools return
  `storage_not_configured` and nothing else is affected. See
  `docs/recipes-capella.md`.

Mount every MCP app behind `IdentityMiddleware`, requiring verified Clerk OAuth
with resource audience and client identifier. Legacy headers and static tokens are rejected. Do not create separate
backend services for Calendar/Drive/Tasks just to support product selection.

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
  audit/            Capella audit writer (30-day expiry) + input redaction (summarize_input)
  providers/        provider-agnostic ToolSpec/registry + provider modules
    google/
      client.py         authorized Google API service builders
      connections.py    connection/token persistence + refresh
      calendar/         read.py (read tools), write.py (mutating tools), common.py
    recipes/          Capella-backed recipe book (models, read, write, common)
  storage/capella/  lazy shared Capella client, OwnedDocumentRepository, storage errors
  access/           client grants, `/access` permissions page, `/connections` status page
migrations/         Alembic migrations
tests/              pytest suite (some DB-backed, auto-skip without Postgres)
scripts/            local dev helpers; capella_admin.py (check/indexes/export, operator-only)
```

## Setup & common commands

Dev tooling runs in the host virtualenv (`.venv`), **not** in the Docker image —
the image is the lean runtime (`uv sync --no-dev`, no tests, no pytest/ruff).

```bash
uv sync                                  # install deps into .venv (incl. dev)
cp .env.example .env                     # then fill in real values
# provision the local Postgres role + db (credentials per .env):
sudo -u postgres psql -c "CREATE ROLE workspace_mcp LOGIN PASSWORD 'wmcp_local_dev_pw';" \
                      -c "CREATE DATABASE workspace_mcp OWNER workspace_mcp;"
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
  module's `register(registry)` entrypoint, wired through a `Connector` entry in
  `connectors/registry.py`. Mounting, tool filtering and error classification
  derive from that list.
- **One uniform pipeline** (`providers/registry.py`) wraps every call:
  resolve identity → validate input → policy/confirm → execute handler →
  queue audit. This is the single chokepoint for identity enforcement and audit.
- **Risk levels.** `RiskLevel.READ` executes directly. `RiskLevel.MUTATING` tools
  have a tier (`RiskTier`): `LOW` executes directly, `MEDIUM` asks the user in chat.
  `ToolSpec.default_tier` is MEDIUM unless `requires_confirmation=False`; each user may
  override it per tool (`user_tool_settings`, set on `/access`, applies to every
  OAuth client; `access.policy.effective_tier`). A MEDIUM call returns
  `confirmation_required` with a `question`, `instructions` and a signed
  `confirmation_token` (`CONFIRMATION_TOKEN_TTL_SECONDS`, 600): the model must ask the
  user, and only after an explicit yes re-invoke with that token. Every mutating tool
  therefore accepts `confirmation_token`. Provide a `preview_builder` on the spec for a
  human-readable question. Outward-facing actions (sending email, etc.) must default to
  MEDIUM.
- **Audit must never leak secrets.** `audit/log.py` summarizes input from a
  per-tool whitelist of safe scalar fields; free-text (queries, summaries,
  descriptions) and tokens are dropped. Add a `_SAFE_FIELDS` entry for each new
  tool — default is "record nothing".
- **One audit document per call**, including failures, written to Capella (scope
  `CAPELLA_AUDIT_SCOPE`, collection `CAPELLA_AUDIT_COLLECTION`) with a per-document
  expiry of `AUDIT_RETENTION_DAYS` (30). Records are queued after the call's outcome is
  known and written best-effort on a background thread (`audit.log.record_audit`): an
  audit failure or an unconfigured Capella never fails a tool call (Capella unset =
  audit is disabled with one warning). There is no Postgres audit table.
- **Tokens are encrypted at rest** (Fernet) and refreshed under a row lock.
  Open WebUI never receives Google tokens.
- **Document storage (Capella).** Storage-backed providers use
  `storage/capella.OwnedDocumentRepository` — never the SDK directly. It builds
  owner-scoped keys (`user::<uuid>::<type>::<uuid>`), verifies ownership on
  read (mismatch = `not_found`), injects `d.owner_user_id = $owner` into every
  SQL++ query, and binds all values as named parameters. The owner is always
  `CallContext.user_id`; never accept it from tool input. Never expose raw
  SQL++, backup, or admin operations as tools. Add one scope per domain in the
  shared bucket and document any index in `scripts/capella/`. Storage errors
  carry stable codes; never return SDK exceptions or connection details.
- **Tool naming** is provider-prefixed: `google_calendar_list_events`, etc. The full rules
  (verbs, mounts, compatibility) are in `spec.md` §Tool Naming. Bare-name mounts
  (`Connector.google_names`) are an alias layer only: write descriptions and field help with
  internal names and the mount rewrites them (`providers.registry.resolve_names`). Never name a
  tool from another connector in model-visible text; say what to pass ("a file_ref from an email
  attachment download"). The one exception is `ToolSpec.related_tools`: a sentence naming another
  tool that is appended only on mounts that serve that tool. `tests/test_tool_discovery.py` fails when a mount's text names a tool
  that is not on it. Two tools may not share an exposed name on one mount, and an alias for an
  unregistered tool fails construction.
- **Discovery schema.** `tools/list` advertises the provider input model's JSON schema
  (`providers.registry.published_schema`): field descriptions, limits, enums and defaults, plus
  `confirmation_token` on mutating tools. Put guidance in `Field(description=...)` and limits in
  the model. Validation still happens only in the pipeline (`invalid_input`, audited), never in
  FastMCP.
- **Minimal OAuth scopes.** Only request scopes for tools actually registered in
  `build_mcp()` (incremental authorization). Product-specific OAuth starts use
  the relevant product scopes only. When you wire up a new provider module, union
  its scopes into `DEFAULT_SCOPES` and enable it in `OAUTH_ENABLED_PRODUCTS` in
  `oauth/google.py` at the same time — not before.

## Adding a tool (checklist)

1. Define a pydantic input model and a handler `(model, ctx, session) -> result`.
2. Build the authorized client via `providers/google/client.py`.
3. Register a `ToolSpec` with the right `RiskLevel` (+ `preview_builder` if mutating).
4. Add a `_SAFE_FIELDS` whitelist entry in `audit/log.py`.
5. If it needs a new Google scope, add it to `oauth/google.py` and `DEFAULT_SCOPES`.
6. Wire the module's `register()` into a `Connector` in `connectors/registry.py`.
7. Add tests; run pytest + ruff.

## House rules

- Match the surrounding style: module docstrings, type hints, `from __future__
  import annotations`, ruff line length 100.
- Keep ruff clean and tests green before considering work done.
- Never log or persist raw tokens, file contents, or free-text event fields.
- Use Conventional Commits (`feat:`, `fix:`, `chore:`, `test:`, `docs:`, ...).

## OAuth client permissions

Client grants are owner-scoped and OAuth-client-scoped explicit tool allowlists.
Every connector keeps its own `/mcp/<slug>` mount; `/mcp` remains compatible.
Filter discovery and enforce grants in the uniform pipeline before confirmation.
Only the interactive browser session may manage grants (with CSRF protection).
New tools are denied until selected. Never reintroduce key/header authentication.

## Connector access modes

`Connector.access_mode` defaults to `AccessMode.PERSONAL`: keep explicit tool grants
for user-owned data, including Recipes. Market (`/mcp/finance`) explicitly uses
`AccessMode.AUTHENTICATED_SHARED` for generic read-only data available to all signed-in
OAuth users. Never infer shared access from absent upstream credentials. Shared
connectors must contain only READ tools; MCP construction validates this invariant.
