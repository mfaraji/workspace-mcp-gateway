# Proposal: make tool discovery clearer for agents (Apex first)

Status: sections 1-5 implemented (uncommitted at the time of writing). Written from a review of
`e81d587`. The task test in "How to tell it worked" has not been run.

Scope: what an agent can learn from `tools/list` on each mount, with Apex as the first case, and the
tool naming rules that support it. Tool IDs, grants, tiers and audit names do not change.

## Summary

Apex may be harder for an agent to use than it needs to be, but the first cause is missing
information, not the names. Fix discovery first, then judge the names with a task test.

1. Field descriptions and limits are lost between the provider's input model and `tools/list`.
2. Apex tool descriptions don't lead with the decision or the consequence.
3. Bare-name mounts (`/mcp/drive`, `/mcp/gmail`, `/mcp/calendar`) tell the model to call tools by
   names that don't exist on that mount.
4. One audit field is recorded twice, once as a value and once as "present".

## 1. Field guidance is lost in discovery

`ToolRegistry._build_callable` (`src/gateway/providers/registry.py:114-128`) builds a flat callable
from each field's name, type and default. Types and enums survive. Descriptions and field limits do
not.

Measured on `apex_transactions_summarize`, discovered with local grant fixtures and no provider calls:

| | Provider model | `tools/list` |
|---|---|---|
| Properties with a description | 23 of 23 | 0 of 23 |
| `group_by` limit | at most 2 items | none |
| `category_ids` limit | at most 20 items | none |

Runtime validation still enforces the limits, so the agent finds out only after a failed call.

**Change.** Have the registration module publish the provider model's description, limits and
defaults, keeping flat inputs and the extra `confirmation_token` on mutating tools. The cost is one
translation in one module. It benefits every provider and every mount.

**Check first.** `WORKSPACE_ID_DESC` in `providers/apex/client.py:44` points at `apex_list_workspaces`.
The Wealth tools reuse it (`providers/apex/wealth.py:16-27`) but that tool isn't on the Wealth mount,
which has `apex_wealth_list_workspaces`. Correct the hint when descriptions start reaching agents.

**Tests.** Assert on the `tools/list` result for a known description, limit, enum, default, and the
confirmation field on a mutating tool. Keep the existing runtime rejection tests.

## 2. Describe Apex tools by the decision they support

- **Transactions mount.** The tool descriptions already carry the workflow. Keep it: find the
  category ID, then summarize, then search for rows. State up front that `summarize` answers "how
  much" and `search` answers "which transactions", and that totals come from Apex and are never
  computed by the model.
- **Invoice status.** `apex_update_invoice_status` with `SENT` emails the invoice PDF to the client;
  `PAID` records a payment. Both facts are in the description today
  (`providers/apex/write.py:353-364`). Put them in the first sentence and in the confirmation preview.
- **Expenses.** `apex_create_expense` books a needs-review expense in the chosen workspace when amount
  and date are known (given or scanned). Say so right after the tested file-reference preamble
  (`tests/test_apex_expenses.py:545-552` pins its opening). `apex_list_expense_categories` is
  read-only, so its approval note (`_INBOX_NOTE`) doesn't belong there.
- **Workspace choice.** The transactions listing already tells the agent to ask the user when the
  workspace is unclear. Use the same rule on the other two Apex mounts.
- **Mount jobs.** One line each: `/mcp/apex` is bookkeeping, `/mcp/apex-transactions` answers
  spending questions, `/mcp/apex-wealth` is personal wealth.

## 3. Names in guidance must exist on the mount

Calendar, Drive and Gmail expose Google's bare names on their own mounts, with prefixed internal
names kept for grants, tiers and audit (`connectors/registry.py:71-99`). Description text is written
once, with the prefixed names, so the model is told to call tools that aren't there:

- `/mcp/drive`, `download_file_content`: "e.g. google_drive_create_file", but the tool is `create_file`
  (`providers/google/drive/read.py:428-433`). Verified with both tools granted: discovery lists
  `create_file` only.
- `/mcp/gmail`, `download_gmail_attachment`: "Get attachment_id from google_gmail_get_message", but
  the tool is `get_message`; it also names `apex_create_expense`, which isn't on that mount
  (`providers/google/gmail/read.py:415-421`).
- `CreateFileInput.file_ref` names `google_gmail_download_attachment` and
  `google_drive_download_file_content` (`providers/google/drive/write.py:78`). Apex expenses also name
  the Gmail tool (`providers/apex/expenses.py:155,184,370`), which is valid only on `/mcp` and
  `/mcp/google`.
- Runtime text uses the internal name too: `"{spec.name} failed"` (`providers/registry.py:316`), the
  reauth guidance (`providers/registry.py:336`) and the confirmation fallback (`policy/confirm.py:104`).

**Change.** Let the mount module resolve names for model-visible text, using the same mapping that
registers the tools. Where the target isn't on the mount, describe what to pass rather than naming a
tool. A mount-neutral sentence is enough when no specific follow-up is needed.

**Guardrails** (none are observed bugs today):

- Reject two tools with the same exposed name on one mount. `gateway_exposed`
  (`mcp/server.py:171-176`) is a dict comprehension, so a duplicate would silently replace an entry,
  and `list_tools` looks grants up through it (`mcp/server.py:55-57`).
- Fail construction when the alias map names a tool that isn't registered. Today
  `register_all` skips it silently (`providers/registry.py:90-93`), giving a quietly smaller mount.
- Extend the bare-mount tests. Calendar tests grant-gated discovery (`test_calendar_google_parity.py:326-348`);
  Drive and Gmail don't.

**Open question, answered.** `download_gmail_attachment` came in with the Google-compatible mounts
(`57e8968`). It matches the name Google's own Gmail MCP server exposes (the claude.ai Gmail connector
lists a tool of that name), so it is Google's name; our internal tool is `download_attachment`. The
comment in `connectors/registry.py` and the parity test now say so. It is not in `GOOGLE_GMAIL_TOOLS`
only because that list holds names identical to ours.

## 4. Naming convention to write down

Add a short section next to `spec.md` §Tool Naming and `AGENTS.md`'s tool naming rule:

- **Identity.** Published canonical IDs stay as they are. New gateway-owned tools use lowercase
  snake_case: `<provider>_[<product>_]<verb>[_<object>]`. Omit the object when the namespace already
  names it, so `apex_transactions_summarize` stays as is.
- **Verbs.** `list` enumerates. `find` resolves a phrase to candidates, such as a category ID.
  `search` returns matching rows. `summarize` returns provider-calculated totals. For a new write,
  pick the verb the user would be authorizing.
- **Mounts.** `/mcp` and `/mcp/google` keep prefixed names. Calendar, Drive and Gmail keep explicit
  bare aliases on their own mounts. Apex and Tasks stay prefixed. `system_get_current_time` is
  unchanged. No alias just for symmetry.
- **Compatibility.** Never rename stored IDs or list old and new synonyms side by side. If tool-choice
  tests later justify an alias, scope it to one mount and resolve it to the existing grant. Never
  migrate permissions by matching display text.

## 5. Audit: one rule per field (separate change)

`google_calendar_suggest_time` lists `time_min`, `time_max` and `time_zone` as safe fields, and the
shared summary rule also records `time_min`/`time_max` as `<set>` (`audit/log.py:118-125,245-247`).
Both run. Verified with a synthetic input:

```
time_min='2030-01-01T09:00:00Z', time_min=<set>
```

Give each field one disposition: drop, presence only, or approved value. Redact before storage. Keep
the approved ID and enum lists for Apex tools. Test that the literal value is absent, not only that
the marker is present. Treat this as its own change. It is unrelated to naming.

## How to tell it worked

Give an agent only the advertised tools and these prompts. Score the route, workspace, tool,
arguments and any unintended write. Use read-only calls and simulated writes.

1. "How much did I spend on groceries last month?"
2. "Show me the transactions behind that total."
3. "Mark this invoice paid without emailing anyone."
4. "Book this receipt to my business." (more than one workspace available)

If wrong choices persist after sections 1-3, revisit wording or names with those results in hand.

## Order of work

1. Section 1, starting with the 23-field transaction summary. Smallest change, widest effect.
2. Section 2, as description edits only.
3. Section 3, with its guardrails.
4. Section 4 as documentation. Section 5 whenever convenient.

## Not covered

No `CONTEXT.md` or `docs/adr/` exists, so no recorded decisions were reopened. The review used local
source and local grant fixtures: no provider calls and no real financial records. Six focused suites
(152 tests) passed with test-only settings. The full suite, widget tests and a live-model selection
run were not done.
