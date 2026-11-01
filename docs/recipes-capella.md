# Recipes connector and Couchbase Capella storage

The `recipes` connector is a personal recipe book for each gateway user. It is
the first consumer of the gateway's reusable Couchbase Capella document storage
(`src/gateway/storage/capella`). It runs inside the existing gateway process:
same identity middleware, policy/confirmation layer, and PostgreSQL audit log.
It has no separate deployment or OAuth flow.

## Endpoints and tools

| Endpoint | Tools |
| --- | --- |
| `/mcp/recipes/` | `system_get_current_time` plus the six `recipes_*` tools |
| `/mcp/` | every tool, including the recipe tools |

| Tool | Risk | Confirmation | Purpose |
| --- | --- | --- | --- |
| `recipes_get` | read | — | Full recipe plus its `version` |
| `recipes_list` | read | — | Summaries, newest first, with optional `category`/`tag`, paginated |
| `recipes_search` | read | — | Free text and/or `ingredients`/`cuisine`/`category`/`tag`, ranked |
| `recipes_save` | mutating | no | Create (insert-only; server-generated id) |
| `recipes_update` | mutating | no | Partial update, CAS-checked via `version` |
| `recipes_delete` | mutating | **yes** | Two-step confirm-token delete |

Save and update skip confirmation (`requires_confirmation=False`, the same
mechanism `telegram_send_message` uses). They only touch the caller's own
recipes and are recoverable. Delete can't be undone, so it keeps the gateway's
default preview + `confirmation_token` flow.

Error codes are stable: `not_found`, `invalid_input`, `version_conflict`,
`already_exists`, `storage_not_configured`, `storage_misconfigured`,
`storage_unavailable`, and `storage_outcome_unknown` (a write timed out
ambiguously; re-read before retrying). Raw SDK errors, connection strings and
credentials are never returned or logged.

## Data model and isolation

- Namespace: bucket `CAPELLA_BUCKET` (default `personal`), scope `recipes`,
  collection `recipes`. Future domains get their own scope in the same bucket.
- Document key: `user::<gateway-user-uuid>::recipe::<recipe-uuid>`.
- Owner = `CallContext.user_id`, the gateway's canonical user UUID. Tools never
  accept `owner_user_id`, `id` or timestamps from clients (inputs use
  `extra="forbid"`).
- `OwnedDocumentRepository` enforces isolation for every provider that uses it:
  - keys are always built from the caller's own UUID;
  - each read verifies the stored `owner_user_id` and reports a mismatch as
    `not_found`, never as "forbidden";
  - each SQL++ statement gets `d.owner_user_id = $owner` added by the
    repository, and every value is passed as a named parameter.
- No tool runs arbitrary SQL++.
- Stored fields: `id`, `owner_user_id`, `schema_version` (1), `title`,
  `description`, `language` (e.g. `en`, `fa`), `cuisine`, `category`,
  `servings`, `ingredients[] {name, quantity, quantity_text, unit, notes}`,
  `instructions[]` (ordered), `prep_time_minutes`, `cook_time_minutes`, `tags`,
  `source_url`, `personal_notes`, `created_at`, `updated_at` (ISO-8601 UTC).
  There is also a derived `searchable` object (normalized copies used for
  matching), which is never returned to clients.
- Quantities can be numbers or text (`"1 1/2"`, `"½"`, `"۱/۲"`, `"2,5"`). Text
  is parsed to a number when possible, and the original text is kept in
  `quantity_text`. Units are stored exactly as written; they are never
  converted.

## Search behaviour and limitations

The connector uses plain SQL++ (`CONTAINS`/`POSITION`/`ANY … SATISFIES`) over
normalized text. It doesn't use Full Text Search, embeddings or a vector store.

- Normalization applies NFKC, case folding, Arabic→Persian letter unification
  (ي→ی, ك→ک, ة→ه, أ/إ/آ→ا), and removes ZWNJ, tatweel and diacritics. As a
  result, `قورمه‌سبزی`, `قورمه سبزی`, and Arabic-keyboard spellings all match.
- Free-text queries are split into up to 5 words, and every word must appear
  somewhere in the title, tags, ingredient names, cuisine or category.
- Ranking: exact title > title prefix > title substring > exact tag >
  ingredient > tag substring > cuisine/category. Ties are broken by most
  recently updated, then by id, so pagination is deterministic.
- Limitations:
  - Matching is substring-only: no stemming ("tomatoes" ≠ "tomato"), no
    synonyms, no typo tolerance.
  - There is no Persian morphological analysis and no transliteration
    ("ghormeh" does not match "قورمه").
  - A short term can match inside a longer word.
- Cost: the single index `(owner_user_id, updated_at DESC, id)` bounds each scan
  to one user's documents. Filtering inside that user's documents isn't
  index-assisted, which is fine for personal recipe books (hundreds to a few
  thousand recipes per user). Collection-wide scans are impossible because
  there's no primary index.

## Partial-failure behaviour (Capella + PostgreSQL)

Capella and PostgreSQL don't share a transaction. The pipeline writes to
Capella inside the handler, then commits the audit row. If that commit fails,
the recipe change is already stored, but the client gets `internal_error`.
Retries are safe:

- **`recipes_save`:** pass an `idempotency_key`. The recipe id is derived from
  it (`uuid5(user, key)`), so a retry returns the original recipe with
  `created: false` and creates no duplicate. Without a key, a retry may create
  a second copy.
- **`recipes_update`:** if the requested change is already present, a retry
  with the old `version` returns `already_applied: true` instead of a conflict.
- **`recipes_delete`:** a retry reports `not_found` ("may already have been
  deleted").
- **If Capella fails:** nothing is written to Capella, and the call is audited
  as an error with the storage error code.

No distributed transactions or outbox are used.

## Capella setup (Free Tier)

1. Sign up at <https://cloud.couchbase.com> and create a **Free Tier**
   operational cluster in a region close to the gateway host. Check Capella's
   current Free Tier limits: free clusters may be paused after inactivity. A
   paused cluster shows up as `storage_unavailable`.
2. **Bucket / scope / collection.** Go to *Data Tools → Buckets*. Create or use
   bucket `personal`, then add scope `recipes` and collection `recipes`. The
   gateway never creates these.
3. **Database credentials (RBAC).** Go to *Settings → Cluster Access* and
   create a database user for the gateway with **Read/Write** access limited
   to bucket `personal`, scope `recipes`. That role covers KV get, insert,
   replace and remove, plus SQL++ SELECT. Don't use an admin or
   all-buckets credential.
4. **Allowed IP addresses.** Go to *Settings → Allowed IP Addresses* and add
   the gateway host's public egress IP as a `/32` entry
   (`curl -s https://ifconfig.me`). Avoid `0.0.0.0/0`.
5. **Connection string.** Copy it from the cluster's *Connect* tab. It must
   start with `couchbases://` (TLS); `couchbase://` is rejected.
6. **Index.** Run [`scripts/capella/recipes_indexes.sqlpp`](../scripts/capella/recipes_indexes.sqlpp)
   in *Data Tools → Query*, or run
   `uv run python scripts/capella_admin.py create-indexes` with credentials
   that have Query Manage Index. Don't create a primary index.

## Configuration

```dotenv
CAPELLA_CONNECTION_STRING=couchbases://cb.<id>.cloud.couchbase.com
CAPELLA_USERNAME=gateway-recipes
CAPELLA_PASSWORD=...
CAPELLA_BUCKET=personal
CAPELLA_RECIPES_SCOPE=recipes
CAPELLA_RECIPES_COLLECTION=recipes
# Optional timeouts (seconds)
CAPELLA_CONNECT_TIMEOUT_SECONDS=10
CAPELLA_KV_TIMEOUT_SECONDS=5
CAPELLA_QUERY_TIMEOUT_SECONDS=15
```

Capella is optional. If these variables are unset, the gateway still starts,
the recipe tools return `storage_not_configured`, and every other connector
works normally.

The client connects lazily on the first recipe call and reuses that
connection. After a failed connect, calls fail fast for 5 seconds before
retrying. The connection closes on application shutdown.

**Docker:** `docker-compose.yml` passes the `CAPELLA_*` variables through from
the host environment or `.env`. The container uses host networking, so its
egress IP is the host's IP, which is the address to allowlist. The Couchbase
SDK adds about 20 MB RSS once loaded; watch the `256m` `mem_limit` under load.

## Testing the connection

```bash
uv run python scripts/capella_admin.py check
# connected; keyspace `personal`.`recipes`.`recipes`
# KV access ok (collection reachable)
# query ok (owner index usable)

CAPELLA_INTEGRATION_TESTS=1 uv run pytest -q tests/test_capella_integration.py
```

The integration test uses random owner UUIDs and deletes whatever it creates.
The standard `uv run pytest` run never touches Capella.

## Registering the MCP endpoint

- **Open WebUI:** go to *Admin → Settings → Tools* and add a tool server at
  `http://127.0.0.1:8000/mcp/recipes/` with the same `X-Gateway-Auth` and
  forwarded user headers as the other connectors.
- **Claude Desktop / Cursor / other native clients:** use
  `https://<public-host>/mcp/recipes/` with the per-user bearer token
  (`python -m gateway.tokens`) or Clerk OAuth, exactly like other connectors.
  For example:

  ```json
  {
    "mcpServers": {
      "recipes": {
        "url": "https://<public-host>/mcp/recipes/",
        "headers": { "Authorization": "Bearer wmcp_..." }
      }
    }
  }
  ```

## Backup / export

Backups are an operator task, run on the gateway host. They are never exposed
as an MCP tool.

```bash
uv run python scripts/capella_admin.py export --out backups/recipes-$(date +%F).jsonl
uv run python scripts/capella_admin.py export --owner <user-uuid> --out alice.jsonl
```

Each line is `{"key": "...", "doc": {...}}`. The file holds full recipe
contents, so protect it like the PostgreSQL dumps. To restore, use Capella's
*Import* (JSON Lines, key from the `key` field) or `cbimport json --format lines`
into the same keyspace.
