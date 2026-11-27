# Workspace MCP Gateway Spec

## Purpose

Build a multi-user MCP gateway for Open WebUI that lets each user connect their own workspace accounts. The first supported provider is Google Workspace with Calendar, Drive, and Tasks.

The gateway should expose one Streamable HTTP MCP endpoint to Open WebUI while keeping provider integrations modular so Microsoft, Slack, Notion, and internal tools can be added later.

## Initial Scope

### Supported Providers

- Google Workspace

### Supported Google Products

- Google Calendar
- Google Drive
- Google Tasks

### Initial Client

- Open WebUI External Tools using MCP over Streamable HTTP

## Non-Goals For V1

- Unrestricted Gmail support (a label-scoped, draft-only Gmail connector exists; see AGENTS.md)
- Microsoft/Slack/Notion support
- Admin UI for managing connections
- Full document editing
- Unrestricted Drive write access
- Background sync of all user data
- Shared service-account access to user calendars/files

## Architecture

```text
Open WebUI
  -> workspace-mcp-gateway
      -> auth/session layer
      -> MCP tool registry
      -> policy/confirmation layer
      -> provider modules
          -> google
              -> calendar
              -> drive
              -> tasks
      -> Postgres token/audit store
```

The gateway owns user-provider connection state. Open WebUI should not receive Google refresh tokens or provider secrets.

## Auth Model

Each Open WebUI user must authorize their own Google account with OAuth. Tokens are stored encrypted server-side and keyed by Open WebUI user identity plus provider account.

The gateway should support identity passed from Open WebUI through configured headers, for example:

- `X-Open-WebUI-User-Id`
- `X-Open-WebUI-User-Email`
- `X-Open-WebUI-User-Name`

If Open WebUI OAuth-to-MCP identity is available, prefer the verified MCP/OAuth identity over plain headers. Plain headers must only be trusted when requests come from the private Open WebUI network or through a signed gateway/auth proxy.

## Google OAuth Scopes

Request the minimum scopes required. Prefer incremental authorization as new tools are enabled.

Calendar:

- `https://www.googleapis.com/auth/calendar.readonly`
- `https://www.googleapis.com/auth/calendar.events`

Drive:

- `https://www.googleapis.com/auth/drive.metadata.readonly`
- `https://www.googleapis.com/auth/drive.readonly`
- `https://www.googleapis.com/auth/drive.file`

Tasks:

- `https://www.googleapis.com/auth/tasks.readonly`
- `https://www.googleapis.com/auth/tasks`

V1 should avoid broad full-Drive write access unless a tool explicitly needs it.

## Tool Naming

Use provider-prefixed tool names so the model can choose tools reliably and future providers do not collide.

Calendar:

- `google_calendar_list_calendars`
- `google_calendar_list_events`
- `google_calendar_get_event`
- `google_calendar_create_event`
- `google_calendar_update_event`
- `google_calendar_delete_event`

Drive:

- `google_drive_search_files`
- `google_drive_get_file_metadata`
- `google_drive_export_file_text`
- `google_drive_list_folder`
- `google_drive_create_text_file`

Tasks:

- `google_tasks_list_tasklists`
- `google_tasks_list_tasks`
- `google_tasks_create_task`
- `google_tasks_update_task`
- `google_tasks_complete_task`
- `google_tasks_delete_task`

### Naming convention

- **Identity.** Published canonical IDs stay as they are. New gateway-owned tools use
  lowercase snake_case: `<provider>_[<product>_]<verb>[_<object>]`. Omit the object when the
  namespace already names it, so `apex_transactions_summarize` stays as is.
- **Verbs.** `list` enumerates. `find` resolves a phrase to candidates, such as a category ID.
  `search` returns matching rows. `summarize` returns provider-calculated totals. For a new
  write, pick the verb the user would be authorizing.
- **Mounts.** `/mcp` and `/mcp/google` keep prefixed names. Calendar, Drive and Gmail keep
  explicit bare aliases on their own mounts (Google's names, `Connector.google_names`). Apex and
  Tasks stay prefixed. `system_get_current_time` is unchanged. No alias just for symmetry.
- **Text names tools as the mount does.** Descriptions and field help are written with internal
  names; a bare mount rewrites them to its exposed names. Text must not name a tool that is not
  on the mount: describe what to pass instead (for example "a file_ref from an email attachment
  download"). A test checks every mount.
- **Compatibility.** Never rename stored IDs or list old and new synonyms side by side. If
  tool-choice tests later justify an alias, scope it to one mount and resolve it to the existing
  grant. Never migrate permissions by matching display text.

## Risk Controls

Read-only tools may execute directly after user authorization.

Mutating tools should require explicit confirmation for V1:

- create calendar event
- update calendar event
- delete calendar event
- create Drive file
- update task
- complete task
- delete task

High-risk Drive tools such as sharing, deleting, or moving files should not be included in V1.

## Data Model

Use Postgres.

### `users`

- `id`
- `external_user_id`
- `email`
- `display_name`
- `created_at`
- `updated_at`

### `provider_connections`

- `id`
- `user_id`
- `provider`
- `provider_account_id`
- `provider_email`
- `scopes`
- `status`
- `created_at`
- `updated_at`
- `last_used_at`

### `provider_tokens`

- `id`
- `connection_id`
- `encrypted_access_token`
- `encrypted_refresh_token`
- `expires_at`
- `created_at`
- `updated_at`

### Tool audit log (Capella, 30-day expiry; formerly the Postgres `tool_audit_log` table)

- `id`
- `user_id`
- `provider`
- `tool_name`
- `request_id`
- `input_summary`
- `result_status`
- `error_code`
- `created_at`

Do not log raw file contents, access tokens, refresh tokens, or full event descriptions by default.

## API Surface

### MCP

- `GET /mcp`
- `POST /mcp`

Use Streamable HTTP MCP.

### Health

- `GET /health`
- `GET /ready`

### OAuth

- `GET /oauth/google/start`
- `GET /oauth/google/callback`
- `POST /oauth/google/disconnect`

## Open WebUI Integration

Admin adds one External Tool:

```text
Type: MCP (Streamable HTTP)
URL: https://workspace-mcp.example.com/mcp
Auth: gateway-specific, or OAuth if supported end to end
```

Recommended headers:

```text
X-Open-WebUI-User-Id: {{USER_ID}}
X-Open-WebUI-User-Email: {{USER_EMAIL}}
X-Open-WebUI-User-Name: {{USER_NAME}}
X-Open-WebUI-Chat-Id: {{CHAT_ID}}
```

The gateway should reject requests without a valid user identity.

## Deployment

Recommended V1 services:

- `workspace-mcp-gateway`
- Postgres
- reverse proxy with TLS

Required environment variables:

- `DATABASE_URL`
- `BASE_URL`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `TOKEN_ENCRYPTION_KEY`
- `TRUSTED_OPEN_WEBUI_ORIGIN`
- `SESSION_SECRET`

## Implementation Recommendation

Use existing libraries for:

- MCP Streamable HTTP server
- Google OAuth
- Google API clients
- token encryption
- Postgres migrations

Build custom code for:

- Open WebUI user identity mapping
- provider connection model
- tool registry
- policy checks
- audit logging
- provider modules

## V1 Milestones

1. Create MCP gateway skeleton with `/health`, `/ready`, and `/mcp`.
2. Add Postgres migrations for users, provider connections, tokens, and audit logs.
3. Implement Google OAuth connection and encrypted token storage.
4. Add Calendar read tools.
5. Add Calendar create/update/delete tools with confirmation.
6. Add Drive search and metadata tools.
7. Add Drive text export for Docs/plain files where supported.
8. Add Tasks read/create/update/complete tools.
9. Add audit logging for all tool calls.
10. Connect to Open WebUI as an External Tool and test with two separate users.

## Acceptance Criteria

- Two different Open WebUI users can connect different Google accounts.
- User A cannot access User B's Calendar, Drive, or Tasks.
- Calendar list/create/update works for the authorized user.
- Drive search returns only files available to the authorized user.
- Tasks list/create/complete works for the authorized user.
- Refresh tokens are encrypted at rest.
- Tool calls are audited without leaking secrets.
- Stopping one provider module does not require redesigning the gateway.

