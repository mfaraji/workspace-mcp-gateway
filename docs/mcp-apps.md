# MCP Apps (interactive widgets)

Some tools render an interactive widget in chat clients that support
[MCP Apps](https://modelcontextprotocol.io/extensions/apps) (Open WebUI, ChatGPT, Claude): a task
checklist you tick, an agenda, and so on. Clients without support still get the normal text result.

## How it works

1. A tool declares `ui_resource="ui://<area>/<name>"` on its `ToolSpec`. The gateway advertises it as
   `_meta.ui.resourceUri` (plus the legacy `_meta["ui/resourceUri"]`).
2. `gateway/mcp/ui/` serves that URI from `resources/read` as `text/html;profile=mcp-app`. Each widget is
   one HTML file in `widgets/`; `shared/base.css` and `shared/bridge.js` are inlined at load time.
3. The client renders the HTML in a sandboxed iframe and sends it the tool result. For widget tools the
   result carries `structuredContent` (what the widget reads) and the same JSON as text (what the model
   reads). Tool descriptions tell the model not to repeat what the widget shows.
4. Widget actions are ordinary `tools/call` requests on the same MCP connection, so **grants, input
   validation, the confirmation handshake and the audit log all apply exactly as for a model call**.

## Security model

- Widgets add no permissions. A `ui://` resource is listed and readable only if the caller has a grant for
  at least one tool that references it (`GatewayFastMCP.list_resources/read_resource`).
- The sandbox has no network: widgets are self-contained (no CDN, no `fetch`). Tests enforce this and that
  data is never written with `innerHTML`.
- Never render Gmail `body_html`; show text only.
- `ToolSpec.ui_app_callable` sets `_meta.ui.autoApprove` on tools a widget may call. Clients ignore it unless
  their admin trusts this server (Open WebUI: `MCP_APP_TRUSTED_SERVERS=<connection id>`); without it the host
  shows its own "allow this tool call?" dialog on every widget action.
- Reversible actions (tasks add/tick/rename) use `requires_confirmation=False`. Destructive ones keep the
  gateway's confirmation token; the widget shows the preview inline (`app.confirm`) and repeats the call with
  the token (`app.callTool` does this automatically).

## Adding a widget

1. Add `widgets/<name>.html` (use `{{SHARED_CSS}}` / `{{SHARED_JS}}`, `GatewayApp({...})`, build DOM with
   `textContent`; support light/dark, RTL and `fa`/`en` strings) and register the URI in `ui/__init__.py`.
2. Set `ui_resource=` on the tools that produce its data and `ui_app_callable=True` on tools it calls.
3. Make tool results contain what the widget needs (see `trim_task`: `is_completed`).
4. Add a jsdom test in `tests/widgets/` (run `npm install` there once) and a row in `tests/test_widget_harness.py`.

Host notes (Open WebUI v0.13.3): theme is sent once, no CSS variables, default CSP blocks all network,
height must be reported via `ui/notifications/size-changed` (the bridge does it), and the app must complete
`ui/initialize` within 30 s.

## Enabling

Widgets are **off by default**. Set `MCP_APPS_ENABLED=true` to advertise `_meta.ui`, list/read `ui://`
resources and keep the widget notes in tool descriptions. When off, every `/mcp` endpoint returns plain
tool results and exposes no `ui://` resources.
